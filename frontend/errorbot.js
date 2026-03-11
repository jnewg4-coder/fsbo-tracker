/**
 * ErrorBot Browser SDK
 *
 * Lightweight instrumentation for frontend error detection:
 * - Rage clicks (repeated clicks on same element)
 * - Network errors (5xx responses)
 * - Auth bounce loops (repeated 401s)
 * - Spinner timeouts (manual via track())
 * - SSE stalls (manual via track())
 *
 * Usage:
 *   const errorbot = new ErrorBot({
 *     publishableKey: 'pk-...',
 *     tenantId: 'avmlens',
 *     appId: 'avmlens',
 *     endpoint: 'https://errorbot.example.com',
 *   });
 *
 *   // Auto-instruments if options.autoInstrument is true (default)
 *   // Or manually:
 *   errorbot.track('spinner_timeout', { route: '/lookup', duration_ms: 30000 });
 */

(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.ErrorBot = factory();
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var MAX_RETRIES = 2;
  var RETRY_DELAYS = [200, 1000]; // ms
  var BATCH_INTERVAL = 5000; // ms — flush events every 5s
  var MAX_BATCH_SIZE = 20;
  var SESSION_KEY = '_errorbot_session';
  var PATCHED_KEY = '__errorbot_fetch_patched';
  var CLICK_WINDOW = 3000; // ms — rage click detection window
  var CLICK_THRESHOLD = 3; // clicks on same element in window = rage click

  function generateId() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  function getSessionId() {
    try {
      var sid = sessionStorage.getItem(SESSION_KEY);
      if (!sid) {
        sid = generateId();
        sessionStorage.setItem(SESSION_KEY, sid);
      }
      return sid;
    } catch (e) {
      // sessionStorage unavailable (private browsing, iframe sandbox)
      return generateId();
    }
  }

  /**
   * @constructor
   * @param {Object} opts
   * @param {string} opts.publishableKey
   * @param {string} opts.tenantId
   * @param {string} opts.appId
   * @param {string} [opts.endpoint='http://localhost:8100']
   * @param {boolean} [opts.autoInstrument=true]
   * @param {boolean} [opts.enabled=true]
   */
  function ErrorBot(opts) {
    if (!opts || !opts.publishableKey || !opts.tenantId || !opts.appId) {
      console.warn('[ErrorBot] Missing required config (publishableKey, tenantId, appId)');
      this._enabled = false;
      return;
    }

    this._publishableKey = opts.publishableKey;
    this._tenantId = opts.tenantId;
    this._appId = opts.appId;
    this._endpoint = (opts.endpoint || 'http://localhost:8100').replace(/\/$/, '');
    this._enabled = opts.enabled !== false;
    this._userId = opts.userId || null;
    this._sessionId = getSessionId();
    this._queue = [];
    this._clickTracker = {};

    if (this._enabled && opts.autoInstrument !== false) {
      this._setupAutoInstrument();
    }

    // Flush queue periodically
    if (this._enabled) {
      var self = this;
      this._flushTimer = setInterval(function () {
        self._flush();
      }, BATCH_INTERVAL);

      // Flush on page exit via sendBeacon (prevents event loss)
      this._onUnload = function () {
        self._flushBeacon();
      };
      // visibilitychange fires more reliably than beforeunload on mobile
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'hidden') {
          self._flushBeacon();
        }
      });
      window.addEventListener('pagehide', this._onUnload);
    }
  }

  /**
   * Track a browser event.
   * @param {string} eventType - e.g. 'spinner_timeout', 'repeated_click'
   * @param {Object} [details={}]
   */
  ErrorBot.prototype.track = function (eventType, details) {
    if (!this._enabled) return;

    details = details || {};
    var event = {
      tenant_id: this._tenantId,
      app_id: this._appId,
      browser_session_id: this._sessionId,
      user_id: this._userId,
      event_type: eventType,
      route: details.route || window.location.pathname,
      view: details.view || null,
      component: details.component || null,
      target_element: details.target_element || null,
      error_message: details.error_message || null,
      network_status: details.network_status || null,
      duration_ms: details.duration_ms || null,
      repeat_count: details.repeat_count || 1,
      request_id: details.request_id || null,
      has_unsaved_work: details.has_unsaved_work || false,
      auth_state: details.auth_state || null,
      metadata: details.metadata || {},
      timestamp: new Date().toISOString(),
    };

    this._queue.push(event);

    if (this._queue.length >= MAX_BATCH_SIZE) {
      this._flush();
    }
  };

  /**
   * Set the current user ID (after login).
   * @param {string|null} userId
   */
  ErrorBot.prototype.setUserId = function (userId) {
    this._userId = userId;
  };

  /**
   * Get a browser-safe recovery decision for a workflow run.
   * @param {string} workflowRunId
   * @returns {Promise<Object|null>}
   */
  ErrorBot.prototype.getRecovery = function (workflowRunId) {
    if (!this._enabled) return Promise.resolve(null);

    var url = this._endpoint + '/api/v1/recovery/decision?workflow_run_id=' + encodeURIComponent(workflowRunId);
    var headers = {
      'Content-Type': 'application/json',
      'X-Errorbot-Publishable-Key': this._publishableKey,
    };

    return fetch(url, { method: 'POST', headers: headers })
      .then(function (res) {
        if (!res.ok) return null;
        return res.json();
      })
      .catch(function () {
        return null;
      });
  };

  /**
   * Get a browser-safe recovery decision by request_id.
   * Browsers have request_id from error responses, not workflow_run_id.
   * @param {string} requestId
   * @returns {Promise<Object|null>}
   */
  ErrorBot.prototype.getRecoveryByRequestId = function (requestId) {
    if (!this._enabled) return Promise.resolve(null);

    var url = this._endpoint + '/api/v1/recovery/by-request/' + encodeURIComponent(requestId);
    var headers = {
      'X-Errorbot-Session-Id': this._sessionId,
    };

    // Use recovery token if we have one (avoids re-sending publishable key)
    if (this._recoveryToken) {
      headers['X-Errorbot-Recovery-Token'] = this._recoveryToken;
    } else {
      headers['X-Errorbot-Publishable-Key'] = this._publishableKey;
    }

    var self = this;
    return fetch(url, { method: 'GET', headers: headers })
      .then(function (res) {
        if (!res.ok) return null;
        return res.json();
      })
      .then(function (data) {
        if (data && data.recovery_token) {
          self._recoveryToken = data.recovery_token;
        }
        return data;
      })
      .catch(function () {
        return null;
      });
  };

  /**
   * Poll for recovery status by request_id with exponential backoff.
   * Resolves when a non-retrying terminal status is reached, or maxAttempts exceeded.
   * @param {string} requestId
   * @param {Object} [opts]
   * @param {number} [opts.maxAttempts=6]
   * @param {number} [opts.initialDelay=2000] ms
   * @param {function} [opts.onStatus] called with each recovery response
   * @returns {Promise<Object|null>}
   */
  ErrorBot.prototype.pollRecovery = function (requestId, opts) {
    if (!this._enabled) return Promise.resolve(null);

    opts = opts || {};
    var maxAttempts = opts.maxAttempts || 6;
    var delay = opts.initialDelay || 2000;
    var onStatus = opts.onStatus || function () {};
    var self = this;

    function attempt(n, currentDelay) {
      if (n >= maxAttempts) return Promise.resolve(null);

      return new Promise(function (resolve) {
        setTimeout(function () {
          self.getRecoveryByRequestId(requestId).then(function (decision) {
            if (!decision) {
              // Not found yet — keep polling
              resolve(attempt(n + 1, Math.min(currentDelay * 1.5, 10000)));
              return;
            }

            onStatus(decision);

            // Terminal statuses — stop polling
            if (decision.status !== 'retrying') {
              resolve(decision);
              return;
            }

            // Still retrying — keep polling
            resolve(attempt(n + 1, Math.min(currentDelay * 1.5, 10000)));
          });
        }, currentDelay);
      });
    }

    return attempt(0, delay);
  };

  /**
   * Flush queued events to the server via fetch.
   * @private
   */
  ErrorBot.prototype._flush = function () {
    if (this._queue.length === 0) return;

    var events = this._queue.splice(0, MAX_BATCH_SIZE);
    var self = this;

    events.forEach(function (event) {
      self._send(event, 0);
    });
  };

  /**
   * Flush queued events via sendBeacon (for page exit).
   * sendBeacon is guaranteed to survive page unload.
   * @private
   */
  ErrorBot.prototype._flushBeacon = function () {
    if (this._queue.length === 0) return;
    if (typeof navigator === 'undefined' || !navigator.sendBeacon) {
      // Fallback to sync flush if sendBeacon not available
      this._flush();
      return;
    }

    var events = this._queue.splice(0, MAX_BATCH_SIZE);
    var url = this._endpoint + '/api/v1/browser-events';
    var self = this;

    // sendBeacon only supports one request, so send each event
    events.forEach(function (event) {
      try {
        var blob = new Blob(
          [JSON.stringify(event)],
          { type: 'application/json' }
        );
        // sendBeacon can't set custom headers, but the service
        // accepts publishable_key as a query param for beacon fallback
        navigator.sendBeacon(
          url + '?pk=' + encodeURIComponent(self._publishableKey),
          blob
        );
      } catch (e) {
        // Best effort — page is unloading anyway
      }
    });
  };

  /**
   * Send a single event with retry.
   * @private
   */
  ErrorBot.prototype._send = function (event, attempt) {
    var self = this;
    var url = this._endpoint + '/api/v1/browser-events';
    var headers = {
      'Content-Type': 'application/json',
      'X-Errorbot-Publishable-Key': this._publishableKey,
    };

    fetch(url, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(event),
    })
      .then(function (res) {
        if (res.status === 500 && attempt < MAX_RETRIES) {
          setTimeout(function () {
            self._send(event, attempt + 1);
          }, RETRY_DELAYS[attempt] || 1000);
        }
      })
      .catch(function () {
        if (attempt < MAX_RETRIES) {
          setTimeout(function () {
            self._send(event, attempt + 1);
          }, RETRY_DELAYS[attempt] || 1000);
        }
      });
  };

  // ---------------------------------------------------------------------------
  // Auto-instrumentation (idempotent — safe to call multiple times)
  // ---------------------------------------------------------------------------

  ErrorBot.prototype._setupAutoInstrument = function () {
    this._instrumentRageClicks();
    this._instrumentFetch();
  };

  /**
   * Detect rage clicks: 3+ clicks on the same element within 3 seconds.
   * @private
   */
  ErrorBot.prototype._instrumentRageClicks = function () {
    var self = this;
    document.addEventListener('click', function (e) {
      var target = e.target;
      var selector = _getSelector(target);
      var now = Date.now();

      if (!self._clickTracker[selector]) {
        self._clickTracker[selector] = { count: 0, firstClick: now };
      }

      var tracker = self._clickTracker[selector];

      if (now - tracker.firstClick > CLICK_WINDOW) {
        tracker.count = 0;
        tracker.firstClick = now;
      }

      tracker.count++;

      if (tracker.count === CLICK_THRESHOLD) {
        self.track('repeated_click', {
          target_element: selector,
          repeat_count: tracker.count,
          duration_ms: now - tracker.firstClick,
        });
        tracker.count = 0;
      }
    }, true);
  };

  /**
   * Patch window.fetch ONCE to detect network errors and auth bounces.
   * Idempotent — checks PATCHED_KEY flag before patching.
   * @private
   */
  ErrorBot.prototype._instrumentFetch = function () {
    if (typeof window === 'undefined' || !window.fetch) return;

    // Idempotent guard: only patch once even if multiple instances exist
    if (window[PATCHED_KEY]) {
      // Already patched — register this instance as a listener
      window[PATCHED_KEY].listeners.push(this);
      return;
    }

    var origFetch = window.fetch;
    var listeners = [this];
    var last401 = null;

    window[PATCHED_KEY] = { listeners: listeners, origFetch: origFetch };

    window.fetch = function () {
      var url = arguments[0];
      var args = arguments;

      // Check if this is an ErrorBot call (skip instrumentation)
      var isErrorBotCall = false;
      for (var i = 0; i < listeners.length; i++) {
        if (typeof url === 'string' && url.indexOf(listeners[i]._endpoint) !== -1) {
          isErrorBotCall = true;
          break;
        }
      }

      if (isErrorBotCall) {
        return origFetch.apply(this, args);
      }

      return origFetch.apply(this, args).then(function (response) {
        // Network errors (5xx)
        if (response.status >= 500) {
          for (var i = 0; i < listeners.length; i++) {
            listeners[i].track('network_error', {
              error_message: response.status + ' ' + response.statusText,
              network_status: response.status,
              target_element: typeof url === 'string' ? url.split('?')[0] : null,
            });
          }
        }

        // Auth bounce (repeated 401s within 5s)
        if (response.status === 401) {
          if (last401 && Date.now() - last401 < 5000) {
            for (var j = 0; j < listeners.length; j++) {
              listeners[j].track('auth_bounce', {
                auth_state: 'expired',
                error_message: 'Repeated 401 responses detected',
              });
            }
          }
          last401 = Date.now();
        }

        return response;
      }).catch(function (err) {
        for (var i = 0; i < listeners.length; i++) {
          listeners[i].track('network_error', {
            error_message: err.message || 'Network request failed',
            target_element: typeof url === 'string' ? url.split('?')[0] : null,
          });
        }
        throw err;
      });
    };
  };

  /**
   * Flush remaining events and clean up.
   */
  ErrorBot.prototype.destroy = function () {
    this._flush();
    if (this._flushTimer) {
      clearInterval(this._flushTimer);
    }
    if (this._onUnload) {
      window.removeEventListener('pagehide', this._onUnload);
    }

    // Remove from fetch listener list
    if (typeof window !== 'undefined' && window[PATCHED_KEY]) {
      var idx = window[PATCHED_KEY].listeners.indexOf(this);
      if (idx !== -1) {
        window[PATCHED_KEY].listeners.splice(idx, 1);
      }
      // If no listeners left, restore original fetch
      if (window[PATCHED_KEY].listeners.length === 0) {
        window.fetch = window[PATCHED_KEY].origFetch;
        delete window[PATCHED_KEY];
      }
    }

    this._enabled = false;
  };

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function _getSelector(el) {
    if (!el || !el.tagName) return 'unknown';
    var tag = el.tagName.toLowerCase();
    if (el.id) return tag + '#' + el.id;
    if (el.className && typeof el.className === 'string') {
      return tag + '.' + el.className.split(' ').slice(0, 2).join('.');
    }
    return tag;
  }

  return ErrorBot;
});
