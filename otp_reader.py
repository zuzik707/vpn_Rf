"""
OTP reader for VFS email verification.
Supports mailto.plus (primary) and generic IMAP fallback.
"""

import asyncio
import logging
import re
import time

import aiohttp

logger = logging.getLogger(__name__)

MAILTO_PLUS_API = "https://tempmail.plus/api/mails"


async def fetch_otp_mailto_plus(email: str, epin: str = "",
                                 timeout: int = 90, poll_interval: int = 5) -> str | None:
    """Poll mailto.plus API for VFS OTP email. Returns 6-digit OTP or None."""
    username = email.split("@")[0]
    start = time.time()
    seen_ids: set[str] = set()

    async with aiohttp.ClientSession() as session:
        # Snapshot existing emails so we only look at new ones
        try:
            params = {"email": username, "limit": 10, "epin": epin}
            async with session.get(MAILTO_PLUS_API, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for mail in data.get("mail_list") or []:
                        seen_ids.add(str(mail.get("mail_id", "")))
        except Exception as e:
            logger.debug("mailto.plus snapshot error: %s", e)

        while time.time() - start < timeout:
            await asyncio.sleep(poll_interval)
            try:
                params = {"email": username, "limit": 5, "epin": epin}
                async with session.get(MAILTO_PLUS_API, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning("mailto.plus API %d", resp.status)
                        continue
                    data = await resp.json()
                    for mail in data.get("mail_list") or []:
                        mail_id = str(mail.get("mail_id", ""))
                        if mail_id in seen_ids:
                            continue
                        subject = (mail.get("subject") or "").lower()
                        text = mail.get("text") or mail.get("body") or ""
                        if "otp" in subject or "one-time" in subject or "verification" in subject or "vfs" in subject:
                            otp = _extract_otp(text)
                            if otp:
                                logger.info("OTP found: %s (from mail %s)", otp, mail_id)
                                return otp
                        # Also check mail body even if subject doesn't match
                        if "vfs" in text.lower() or "one-time password" in text.lower():
                            otp = _extract_otp(text)
                            if otp:
                                logger.info("OTP found in body: %s", otp)
                                return otp
                        seen_ids.add(mail_id)
            except asyncio.TimeoutError:
                logger.debug("mailto.plus poll timeout")
            except Exception as e:
                logger.warning("mailto.plus poll error: %s", e)

    logger.error("OTP not received within %ds for %s", timeout, email)
    return None


async def fetch_otp_by_detail(email: str, epin: str = "",
                               timeout: int = 90, poll_interval: int = 5) -> str | None:
    """Fetch OTP by reading individual mail detail (more reliable for body extraction)."""
    username = email.split("@")[0]
    start = time.time()
    seen_ids: set[str] = set()

    async with aiohttp.ClientSession() as session:
        # Snapshot
        try:
            params = {"email": username, "limit": 10, "epin": epin}
            async with session.get(MAILTO_PLUS_API, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for mail in data.get("mail_list") or []:
                        seen_ids.add(str(mail.get("mail_id", "")))
        except Exception:
            pass

        while time.time() - start < timeout:
            await asyncio.sleep(poll_interval)
            try:
                params = {"email": username, "limit": 5, "epin": epin}
                async with session.get(MAILTO_PLUS_API, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    for mail in data.get("mail_list") or []:
                        mail_id = str(mail.get("mail_id", ""))
                        if mail_id in seen_ids:
                            continue
                        seen_ids.add(mail_id)

                        # Fetch full mail detail
                        detail_url = f"{MAILTO_PLUS_API}/{mail_id}"
                        detail_params = {"email": username, "epin": epin}
                        async with session.get(detail_url, params=detail_params,
                                               timeout=aiohttp.ClientTimeout(total=15)) as dr:
                            if dr.status != 200:
                                continue
                            detail = await dr.json()
                            body = detail.get("text") or detail.get("body") or detail.get("html") or ""
                            otp = _extract_otp(body)
                            if otp:
                                logger.info("OTP from detail: %s (mail %s)", otp, mail_id)
                                return otp
            except Exception as e:
                logger.debug("mailto.plus detail poll error: %s", e)

    logger.error("OTP not received (detail) within %ds for %s", timeout, email)
    return None


def _extract_otp(text: str) -> str | None:
    """Extract 4-8 digit OTP from email text."""
    if not text:
        return None
    # VFS typically sends 6-digit OTP
    # Look for patterns like "OTP: 123456" or "123456" near OTP context
    patterns = [
        r'(?:otp|one.time.password|verification.code|code)\s*(?:is|:)?\s*(\d{4,8})',
        r'(?:enter|use|submit)\s+(?:the\s+)?(?:code\s+)?(\d{4,8})',
        r'\b(\d{6})\b',  # standalone 6-digit number (most common)
        r'\b(\d{4})\b',  # 4-digit fallback
    ]
    text_lower = text.lower()
    for pattern in patterns:
        m = re.search(pattern, text_lower)
        if m:
            return m.group(1)
    # Last resort: any 6-digit number in the text
    all_nums = re.findall(r'\b(\d{6})\b', text)
    if all_nums:
        return all_nums[0]
    return None


async def get_vfs_otp(email: str, mail_password: str = "",
                       timeout: int = 90) -> str | None:
    """Main entry point: get VFS OTP for the given email address."""
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if domain in ("mailto.plus", "tempmail.plus"):
        otp = await fetch_otp_mailto_plus(email, epin=mail_password, timeout=timeout)
        if not otp:
            otp = await fetch_otp_by_detail(email, epin=mail_password, timeout=timeout)
        return otp
    logger.warning("Unsupported email domain for OTP: %s — trying mailto.plus API anyway", domain)
    return await fetch_otp_mailto_plus(email, epin=mail_password, timeout=timeout)
