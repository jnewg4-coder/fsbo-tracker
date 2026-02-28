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
