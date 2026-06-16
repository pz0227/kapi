"""
Standalone ChatGPT browser login — run this directly from A:\\Kapi_PM:
  python backend/scripts/browser_login.py [session_output.json]

Opens a real Chromium window. Log in to ChatGPT.
Session saved to the specified path (default: backend/sessions/chatgpt.json).

Note: ChatGPT migrated from chat.openai.com → chatgpt.com in 2024.
"""
import asyncio, json, sys, base64
from pathlib import Path

SESSION_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("backend/sessions/chatgpt.json")


def _is_post_login_url(url: str) -> bool:
    """Return True once the user has passed the login/auth screens."""
    if "chatgpt.com" not in url and "chat.openai.com" not in url:
        return False
    # Still on auth pages — keep waiting
    if "/auth/" in url or "/login" in url or "sso.openai.com" in url:
        return False
    return True


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    print("[Kapi] Opening browser → https://chatgpt.com/auth/login")
    print("[Kapi] Please log in to ChatGPT. The window will close automatically once authenticated.")
    print("[Kapi] You have 10 minutes.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50, args=["--start-maximized"])
        ctx     = await browser.new_context(viewport=None)
        page    = await ctx.new_page()
        await page.goto("https://chatgpt.com/auth/login")

        try:
            await page.wait_for_url(_is_post_login_url, timeout=600_000)
        except Exception as e:
            await browser.close()
            print(f"ERROR: Login timed out or failed: {e}")
            sys.exit(1)

        await asyncio.sleep(3)
        cookies      = {c["name"]: c["value"] for c in await ctx.cookies()}
        session_resp = await page.evaluate(
            "fetch('https://chatgpt.com/api/auth/session').then(r=>r.json())"
        )
        access_token = session_resp.get("accessToken", "")
        await browser.close()

        if not access_token:
            print("ERROR: Could not retrieve access token. Make sure you are fully logged in.")
            sys.exit(1)

        def jwt_exp(tok):
            try:
                p = tok.split(".")[1] + "=="
                return json.loads(base64.urlsafe_b64decode(p)).get("exp")
            except Exception:
                return None

        exp  = jwt_exp(access_token)
        data = {"cookies": cookies, "access_token": access_token, "expires_at": None}
        if exp:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(exp, tz=timezone.utc)
            data["expires_at"] = dt.isoformat()
            print(f"[Kapi] Token expires: {dt.strftime('%Y-%m-%d %H:%M UTC')}")

        SESSION_FILE.write_text(json.dumps(data, indent=2))
        print(f"[Kapi] Session saved → {SESSION_FILE}")
        print("[Kapi] Done. You can now use this session in Kapi.")


asyncio.run(main())
