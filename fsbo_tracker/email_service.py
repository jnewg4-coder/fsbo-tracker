"""
FSBO Deal Tracker — Email service (Brevo API).

Sends transactional emails for:
- Email verification (6-digit code)
- Password reset
- Welcome after verification

Ported from AVMLens email_service.py, rebranded for FSBO.
Uses httpx directly — no SDK dependency.
"""

import os
import logging
import httpx

logger = logging.getLogger("fsbo.email")

# Config from env
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
FROM_EMAIL = os.environ.get("FROM_EMAIL", "solutions@fsbotracker.app")
FROM_NAME = os.environ.get("FROM_NAME", "FSBO Deal Tracker")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://fsbotracker.app")
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def _send_email(to_email: str, subject: str, html_content: str) -> bool:
    """Send email via Brevo API. Returns True if successful."""
    if not BREVO_API_KEY:
        logger.warning(f"[EMAIL] BREVO_API_KEY not set. Would send to {to_email}: {subject}")
        print(f"[EMAIL] Dev mode — would send to {to_email}: {subject}")
        return True  # Dev fallback — non-blocking

    payload = {
        "sender": {"email": FROM_EMAIL, "name": FROM_NAME},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content,
    }

    headers = {
        "api-key": BREVO_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(BREVO_API_URL, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        logger.info(f"[EMAIL] Sent to {to_email}: {subject}")
        return True
    except httpx.HTTPStatusError as e:
        logger.error(f"[EMAIL] Brevo API error for {to_email}: {e.response.status_code} - {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"[EMAIL] Failed to send to {to_email}: {e}")
        return False


def send_verification_email(to_email: str, code: str) -> bool:
    """Send 6-digit email verification code."""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #08080d; color: #e4e4ec; padding: 40px; margin: 0; }}
            .container {{ max-width: 500px; margin: 0 auto; background: #101018; border-radius: 12px; padding: 40px; }}
            h1 {{ color: #10b981; margin: 0 0 20px 0; }}
            p {{ line-height: 1.6; color: #9ca3af; margin: 0 0 16px 0; }}
            .code-box {{ background: #08080d; border: 2px solid #10b981; border-radius: 12px; padding: 24px; text-align: center; margin: 24px 0; }}
            .code {{ font-size: 36px; font-weight: 700; color: #10b981; letter-spacing: 8px; font-family: monospace; }}
            .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #1c1c2a; font-size: 12px; color: #555568; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Verify your email</h1>
            <p>Thanks for signing up for FSBO Deal Tracker. Enter this code to activate your account:</p>
            <div class="code-box">
                <div class="code">{code}</div>
            </div>
            <p>This code expires in 24 hours.</p>
            <div class="footer">
                <p>If you didn't create an account, ignore this email.</p>
            </div>
        </div>
    </body>
    </html>
    """
    return _send_email(to_email, "Your FSBO Deal Tracker verification code", html)


def send_password_reset_email(to_email: str, token: str) -> bool:
    """Send password reset link."""
    reset_url = f"{FRONTEND_URL}/app#reset={token}"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #08080d; color: #e4e4ec; padding: 40px; margin: 0; }}
            .container {{ max-width: 500px; margin: 0 auto; background: #101018; border-radius: 12px; padding: 40px; }}
            h1 {{ color: #10b981; margin: 0 0 20px 0; }}
            p {{ line-height: 1.6; color: #9ca3af; margin: 0 0 16px 0; }}
            .button {{ display: inline-block; background: #10b981; color: #08080d !important; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; margin: 20px 0; }}
            .link {{ color: #10b981; word-break: break-all; font-size: 14px; }}
            .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #1c1c2a; font-size: 12px; color: #555568; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Reset your password</h1>
            <p>We received a request to reset your FSBO Deal Tracker password. Click below:</p>
            <p style="text-align: center;">
                <a href="{reset_url}" class="button">Reset Password</a>
            </p>
            <p>Or copy this link:</p>
            <p class="link">{reset_url}</p>
            <p>This link expires in 1 hour. If you didn't request this, ignore this email.</p>
        </div>
    </body>
    </html>
    """
    return _send_email(to_email, "Reset your FSBO Deal Tracker password", html)


def _html_esc(s) -> str:
    """HTML-escape a string for safe injection into email templates."""
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def send_alert_email(to_email: str, search_name: str, listings: list) -> bool:
    """Send deal alert digest email.

    Args:
        to_email: Recipient email.
        search_name: Name of the saved search.
        listings: List of dicts with address, city, state, score, price, beds, baths, sqft.
    """
    count = len(listings)
    safe_name = _html_esc(search_name)
    subject = f"FSBO Alert: {count} new deal{'s' if count != 1 else ''} — {search_name}"

    # Build listing rows
    listing_rows = ""
    for i, l in enumerate(listings[:10]):  # Cap at 10 in email
        price_str = f"${l.get('price', 0):,.0f}" if l.get("price") else "N/A"
        score_str = str(l.get("score", "—"))
        addr = _html_esc(l.get("address", "Unknown"))
        city = _html_esc(l.get("city", ""))
        state = _html_esc(l.get("state", ""))
        beds = l.get("beds") or "—"
        baths = l.get("baths") or "—"
        sqft = f"{l.get('sqft', 0):,}" if l.get("sqft") else "—"

        bg = "#101018" if i % 2 == 0 else "#0c0c14"
        listing_rows += f"""
        <tr style="background: {bg};">
            <td style="padding: 12px; color: #e4e4ec; font-size: 14px;">
                <strong>{addr}</strong><br>
                <span style="color: #9ca3af; font-size: 12px;">{city}, {state}</span>
            </td>
            <td style="padding: 12px; text-align: center;">
                <span style="background: {'#10b981' if (l.get('score') or 0) >= 60 else '#06b6d4' if (l.get('score') or 0) >= 40 else '#555568'}; color: #fff; padding: 4px 10px; border-radius: 12px; font-weight: 600; font-size: 13px;">{score_str}</span>
            </td>
            <td style="padding: 12px; color: #10b981; font-weight: 600; font-size: 14px; text-align: right;">{price_str}</td>
            <td style="padding: 12px; color: #9ca3af; font-size: 13px; text-align: center;">{beds}/{baths} · {sqft}sf</td>
        </tr>"""

    remaining = count - 10
    remaining_note = f'<p style="color: #9ca3af; font-size: 13px; text-align: center; margin-top: 12px;">+ {remaining} more listing{"s" if remaining != 1 else ""}. Open the app to see all.</p>' if remaining > 0 else ""

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #08080d; color: #e4e4ec; padding: 40px; margin: 0; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #101018; border-radius: 12px; padding: 32px; }}
            h1 {{ color: #10b981; margin: 0 0 8px 0; font-size: 22px; }}
            p {{ line-height: 1.6; color: #9ca3af; margin: 0 0 16px 0; }}
            .stat-row {{ display: flex; gap: 16px; margin-bottom: 24px; }}
            .stat {{ background: #08080d; border-radius: 8px; padding: 16px; text-align: center; flex: 1; }}
            .stat-value {{ font-size: 28px; font-weight: 700; color: #10b981; }}
            .stat-label {{ font-size: 12px; color: #555568; text-transform: uppercase; letter-spacing: 1px; }}
            table {{ width: 100%; border-collapse: collapse; border-radius: 8px; overflow: hidden; }}
            th {{ background: #08080d; color: #555568; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; padding: 10px 12px; text-align: left; }}
            .button {{ display: inline-block; background: #10b981; color: #08080d !important; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; margin: 24px 0; }}
            .footer {{ margin-top: 24px; padding-top: 20px; border-top: 1px solid #1c1c2a; font-size: 12px; color: #555568; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>New Deals Found</h1>
            <p>Your saved search <strong style="color: #e4e4ec;">"{safe_name}"</strong> matched {count} new listing{"s" if count != 1 else ""}.</p>

            <table>
                <thead>
                    <tr>
                        <th>Property</th>
                        <th style="text-align: center;">Score</th>
                        <th style="text-align: right;">Price</th>
                        <th style="text-align: center;">Details</th>
                    </tr>
                </thead>
                <tbody>
                    {listing_rows}
                </tbody>
            </table>
            {remaining_note}

            <p style="text-align: center;">
                <a href="{FRONTEND_URL}/app" class="button">Open Deal Tracker</a>
            </p>

            <div class="footer">
                <p>You're receiving this because you have alert notifications enabled for "{safe_name}". <a href="{FRONTEND_URL}/app#settings" style="color: #06b6d4;">Manage preferences</a></p>
            </div>
        </div>
    </body>
    </html>
    """
    return _send_email(to_email, subject, html)


def send_welcome_email(to_email: str) -> bool:
    """Send welcome email after verification."""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #08080d; color: #e4e4ec; padding: 40px; margin: 0; }}
            .container {{ max-width: 500px; margin: 0 auto; background: #101018; border-radius: 12px; padding: 40px; }}
            h1 {{ color: #10b981; margin: 0 0 20px 0; }}
            p {{ line-height: 1.6; color: #9ca3af; margin: 0 0 16px 0; }}
            .button {{ display: inline-block; background: #10b981; color: #08080d !important; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; margin: 20px 0; }}
            ul {{ color: #9ca3af; padding-left: 20px; }}
            li {{ margin: 8px 0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>You're in!</h1>
            <p>Your FSBO Deal Tracker account is active. Here's what you can do:</p>
            <ul>
                <li>Browse FSBO listings across up to 28 US metros</li>
                <li>See distress scores and price-cut history</li>
                <li>Track deals from offer to close</li>
            </ul>
            <p style="text-align: center;">
                <a href="{FRONTEND_URL}/app" class="button">Open Deal Tracker</a>
            </p>
            <p>Questions? Reply to this email.</p>
        </div>
    </body>
    </html>
    """
    return _send_email(to_email, "Welcome to FSBO Deal Tracker!", html)
