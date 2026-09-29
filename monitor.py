from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

CATEGORY_URL = os.getenv(
    "HERMES_CATEGORY_URL",
    "https://www.hermes.com/br/pt/category/artigos-de-couro/bolsas-e-bolsas-clutch/bolsas-e-bolsas-clutch-femininas/?facet_line=picotin_lock",
)
DIRECT_URLS = [
    line.strip()
    for line in os.getenv("HERMES_DIRECT_URLS", "").splitlines()
    if line.strip()
]

TARGET_RE = re.compile(r"\bpicotin\s+lock\s*18\b", re.I)
PRICE_RE = re.compile(r"R\$\s*[\d.]+(?:,\d{2})?")

CHECKS_PER_PRODUCT = max(3, int(os.getenv("CHECKS_PER_PRODUCT", "3")))
BETWEEN_CHECKS_SECONDS = max(0.5, float(os.getenv("BETWEEN_CHECKS_SECONDS", "2")))
NAV_TIMEOUT_MS = max(20_000, int(os.getenv("NAV_TIMEOUT_MS", "60_000")))
HYDRATE_WAIT_MS = max(1_500, int(os.getenv("HYDRATE_WAIT_MS", "4_000")))

PURCHASE_PATTERNS = [
    r"adicionar\s+à\s+sacola",
    r"adicionar\s+ao\s+carrinho",
    r"add\s+to\s+bag",
    r"add\s+to\s+cart",
]
PURCHASE_RE = re.compile("|".join(PURCHASE_PATTERNS), re.I)

NEGATIVE_MARKERS = [
    "infelizmente, este produto não está mais disponível",
    "este produto não está mais disponível",
    "não está disponível",
    "não disponível em estoque",
    "fora de estoque",
    "esgotado",
    "sem estoque",
    "out of stock",
    "sold out",
    "not available",
    "currently unavailable",
]

WAITING_MARKERS = [
    "você será informado(a) assim que este produto estiver disponível em estoque",
    "você será informado assim que este produto estiver disponível em estoque",
    "avise-me quando estiver disponível",
    "notify me when available",
]

BLOCK_MARKERS = [
    "captcha",
    "recaptcha",
    "verifique se você é um humano",
    "access denied",
    "forbidden",
    "temporarily unavailable",
    "just a moment",
    "unusual traffic",
]

DISABLED_CLASS_RE = re.compile(r"\b(disabled|unavailable|is-disabled|sold-out|out-of-stock)\b", re.I)


@dataclass
class CheckEvidence:
    url: str
    title: str
    price: str | None
    status: str
    product_match: bool
    purchase_cta_visible: bool
    purchase_cta_enabled: bool
    purchase_cta_text: str | None
    jsonld_instock: bool
    jsonld_outofstock: bool
    negative_text: bool
    waiting_text: bool
    blocked_page: bool
    reasons: list[str]


@dataclass
class ProductResult:
    url: str
    title: str
    price: str | None
    status: str
    checks_confirmed: str
    checks: list[CheckEvidence]


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    # Strip tracking/query parameters so the Telegram link is clean.
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def with_cache_buster(url: str, seed: int) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_stockcheck={seed}"


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def extract_jsonld_availability(scripts: list[str]) -> list[str]:
    values: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_l = str(key).lower()
                if key_l in {"availability", "itemavailability", "availabilitystatus"}:
                    values.append(str(value))
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for raw in scripts:
        try:
            walk(json.loads(raw))
        except Exception:
            continue
    return values


async def locate_purchase_cta(page: Page) -> tuple[bool, bool, str | None]:
    """Return (visible, enabled, text) for a real purchase CTA."""
    candidates = page.locator("button, a, [role='button']").filter(has_text=PURCHASE_RE)
    count = await candidates.count()
    saw_visible = False

    for i in range(count):
        element = candidates.nth(i)
        try:
            if not await element.is_visible():
                continue
            saw_visible = True
            text = clean_text(await element.inner_text()) or None
            disabled_attr = await element.get_attribute("disabled")
            aria_disabled = clean_text(await element.get_attribute("aria-disabled") or "")
            data_disabled = clean_text(await element.get_attribute("data-disabled") or "")
            class_name = await element.get_attribute("class") or ""
            href = await element.get_attribute("href")

            style = await element.evaluate(
                "el => ({pointerEvents: getComputedStyle(el).pointerEvents, display: getComputedStyle(el).display, visibility: getComputedStyle(el).visibility})"
            )

            enabled = True
            if disabled_attr is not None:
                enabled = False
            if aria_disabled == "true" or data_disabled == "true":
                enabled = False
            if DISABLED_CLASS_RE.search(class_name):
                enabled = False
            if style.get("pointerEvents") == "none":
                enabled = False
            if style.get("display") == "none" or style.get("visibility") == "hidden":
                enabled = False

            # For links, make sure there is an actual target unless the site uses a JS-only CTA.
            if enabled and element.evaluate("el => el.tagName.toLowerCase() === 'a'") and not href:
                # JS-only anchor/button-like elements can still be valid, so do not force-disable.
                pass

            return saw_visible, enabled, text
        except Exception:
            continue

    return saw_visible, False, None


async def open_page(browser: Browser, url: str, check_no: int) -> tuple[Any, Page]:
    context = await browser.new_context(
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        viewport={"width": 1440, "height": 1200},
        color_scheme="light",
        service_workers="block",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    await page.goto(
        with_cache_buster(url, int(time.time() * 1000) + check_no),
        wait_until="domcontentloaded",
        timeout=NAV_TIMEOUT_MS,
    )
    try:
        await page.wait_for_load_state("load", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(HYDRATE_WAIT_MS)
    return context, page


async def check_once(browser: Browser, url: str, check_no: int) -> CheckEvidence:
    context = None
    try:
        context, page = await open_page(browser, url, check_no)
        body = await page.locator("body").inner_text(timeout=20_000)
        body_clean = clean_text(body)

        try:
            h1 = clean_text(await page.locator("h1").first.inner_text(timeout=5_000))
        except Exception:
            h1 = ""
        title = h1 or clean_text(await page.title())

        price_match = PRICE_RE.search(body)
        price = price_match.group(0) if price_match else None

        jsonld_scripts = await page.locator("script[type='application/ld+json']").all_inner_texts()
        availability_values = [clean_text(x) for x in extract_jsonld_availability(jsonld_scripts)]
        jsonld_instock = any("instock" in x and "outofstock" not in x for x in availability_values)
        jsonld_outofstock = any("outofstock" in x or "soldout" in x for x in availability_values)

        purchase_visible, purchase_enabled, purchase_text = await locate_purchase_cta(page)
        negative_text = any(marker in body_clean for marker in NEGATIVE_MARKERS)
        waiting_text = any(marker in body_clean for marker in WAITING_MARKERS)
        blocked_page = any(marker in body_clean[:25_000] for marker in BLOCK_MARKERS)

        # Require the exact product name in the heading or product-area text, not just a generic page title.
        product_match = bool(TARGET_RE.search(h1) or TARGET_RE.search(title) or TARGET_RE.search(body[:12_000]))

        contradictory = negative_text or waiting_text or jsonld_outofstock

        # AVAILABLE: a live purchase CTA is the direct transactional evidence. We also require
        # the page itself to be unblocked and free of explicit stock contradictions.
        available = (
            product_match
            and purchase_visible
            and purchase_enabled
            and not contradictory
            and not blocked_page
        )

        # UNAVAILABLE: only explicit stock-negative evidence counts, and a live CTA must be absent/disabled.
        unavailable = (
            product_match
            and not purchase_enabled
            and (negative_text or waiting_text or jsonld_outofstock)
            and not blocked_page
        )

        status = "AVAILABLE" if available else "UNAVAILABLE" if unavailable else "UNKNOWN"

        reasons = [
            f"product_match={product_match}",
            f"purchase_cta_visible={purchase_visible}",
            f"purchase_cta_enabled={purchase_enabled}",
            f"purchase_cta_text={purchase_text!r}",
            f"jsonld_instock={jsonld_instock}",
            f"jsonld_outofstock={jsonld_outofstock}",
            f"negative_text={negative_text}",
            f"waiting_text={waiting_text}",
            f"blocked_page={blocked_page}",
            f"status={status}",
        ]

        return CheckEvidence(
            url=normalize_url(url),
            title=title,
            price=price,
            status=status,
            product_match=product_match,
            purchase_cta_visible=purchase_visible,
            purchase_cta_enabled=purchase_enabled,
            purchase_cta_text=purchase_text,
            jsonld_instock=jsonld_instock,
            jsonld_outofstock=jsonld_outofstock,
            negative_text=negative_text,
            waiting_text=waiting_text,
            blocked_page=blocked_page,
            reasons=reasons,
        )

    except (PlaywrightTimeoutError, Exception) as exc:
        return CheckEvidence(
            url=normalize_url(url),
            title="",
            price=None,
            status="UNKNOWN",
            product_match=False,
            purchase_cta_visible=False,
            purchase_cta_enabled=False,
            purchase_cta_text=None,
            jsonld_instock=False,
            jsonld_outofstock=False,
            negative_text=False,
            waiting_text=False,
            blocked_page=False,
            reasons=[f"check_error={type(exc).__name__}: {exc}"],
        )
    finally:
        if context is not None:
            await context.close()


async def inspect_product(browser: Browser, url: str) -> ProductResult:
    checks: list[CheckEvidence] = []
    for check_no in range(1, CHECKS_PER_PRODUCT + 1):
        checks.append(await check_once(browser, url, check_no))
        if check_no < CHECKS_PER_PRODUCT:
            await asyncio.sleep(BETWEEN_CHECKS_SECONDS)

    available = sum(c.status == "AVAILABLE" for c in checks)
    unavailable = sum(c.status == "UNAVAILABLE" for c in checks)

    if available == CHECKS_PER_PRODUCT:
        final_status = "AVAILABLE"
        confirmed = f"{available}/{CHECKS_PER_PRODUCT}"
    elif unavailable == CHECKS_PER_PRODUCT:
        final_status = "UNAVAILABLE"
        confirmed = f"{unavailable}/{CHECKS_PER_PRODUCT}"
    else:
        final_status = "UNKNOWN"
        confirmed = f"A={available}, U={unavailable}, ?={CHECKS_PER_PRODUCT - available - unavailable}"

    return ProductResult(
        url=normalize_url(url),
        title=next((c.title for c in reversed(checks) if c.title), "Picotin Lock 18"),
        price=next((c.price for c in reversed(checks) if c.price), None),
        status=final_status,
        checks_confirmed=confirmed,
        checks=checks,
    )


async def discover_product_urls(browser: Browser) -> list[str]:
    context = await browser.new_context(
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        viewport={"width": 1440, "height": 1200},
        color_scheme="light",
        service_workers="block",
    )
    page = await context.new_page()
    try:
        await page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("load", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(HYDRATE_WAIT_MS)

        anchors = await page.locator("a[href]").evaluate_all(
            """
            els => els.map(a => ({
              href: a.href,
              text: (a.innerText || '').trim(),
              aria: a.getAttribute('aria-label') || '',
              title: a.getAttribute('title') || ''
            }))
            """
        )

        found: list[str] = []
        for item in anchors:
            text = clean_text(" ".join(str(item.get(k, "")) for k in ("text", "aria", "title")))
            href = str(item.get("href") or "")
            if TARGET_RE.search(text) and "hermes.com" in href.lower():
                found.append(normalize_url(href))

        for url in DIRECT_URLS:
            found.append(normalize_url(url))

        # A product URL is safer than an arbitrary category link.
        found = [u for u in found if "/product/" in u and TARGET_RE.search(u + " ") or "/product/" in u]
        return list(dict.fromkeys(found))
    finally:
        await context.close()


def telegram_api(token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    data = urllib.parse.urlencode(params or {}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data if params else None,
        method="POST" if params else "GET",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {payload}")
    return payload


def telegram_send(token: str, chat_id: str, message: str) -> None:
    telegram_api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "false",
        },
    )


def auto_discover_private_chat_id(token: str) -> str | None:
    """Find a unique private chat that has messaged the bot.

    We intentionally do not advance Telegram's getUpdates offset, so the same
    onboarding message can still be discovered by later scheduled runs.
    If multiple private chats exist, refuse to guess and require TELEGRAM_CHAT_ID.
    """
    payload = telegram_api(token, "getUpdates", {"limit": "100", "timeout": "0"})
    updates = payload.get("result") or []
    candidates: dict[str, int] = {}

    for update in updates:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            continue
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        key = str(chat_id)
        candidates[key] = max(candidates.get(key, -1), int(update.get("update_id", 0)))

    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        raise RuntimeError(
            "More than one private Telegram chat is associated with this bot. "
            "Set TELEGRAM_CHAT_ID as a GitHub Actions secret so the monitor does not message the wrong person."
        )
    return None


def build_message(results: list[ProductResult], discovery_ok: bool) -> str:
    now = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime())
    lines = [
        "👜 HERMÈS — PICOTIN LOCK 18",
        f"🕒 Verificado: {now} (São Paulo)",
        "",
    ]

    if not discovery_ok:
        lines += [
            "⚠️ NÃO CONFIRMADO — não foi possível validar a página da Hermès.",
        ]
    elif not results:
        lines += [
            "⚠️ NÃO CONFIRMADO — nenhuma página de Picotin Lock 18 foi localizada.",
        ]
    else:
        for item in results:
            if item.status == "AVAILABLE":
                status = "✅ DISPONÍVEL"
            elif item.status == "UNAVAILABLE":
                status = "❌ INDISPONÍVEL"
            else:
                status = "⚠️ NÃO CONFIRMADO"

            lines.append(f"• {item.title}")
            if item.price:
                lines.append(f"  💰 {item.price}")
            lines.append(f"  {status}")
            lines.append(f"  🔗 {item.url}")
            lines.append("")

    lines.append("⏱️ Próxima verificação: aproximadamente 10 min")
    return "\n".join(lines).strip()


async def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        print("Missing TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 2

    if not chat_id:
        try:
            chat_id = auto_discover_private_chat_id(token) or ""
        except Exception as exc:
            print(f"Telegram chat autodiscovery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        if not chat_id:
            print(
                "No private chat found. Open the bot in Telegram, press Start, send 'teste', "
                "and run the workflow once. Or set TELEGRAM_CHAT_ID as a GitHub Actions secret.",
                file=sys.stderr,
            )
            return 2
        print("Telegram chat ID auto-discovered.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            try:
                urls = await discover_product_urls(browser)
                discovery_ok = True
            except Exception as exc:
                print(f"Discovery error: {type(exc).__name__}: {exc}", file=sys.stderr)
                urls = [normalize_url(u) for u in DIRECT_URLS]
                discovery_ok = False

            urls = list(dict.fromkeys(urls))
            print(f"Discovered {len(urls)} candidate Picotin Lock 18 URL(s)")

            results: list[ProductResult] = []
            for url in urls:
                result = await inspect_product(browser, url)
                results.append(result)
                print(json.dumps(asdict(result), ensure_ascii=False, indent=2))

            message = build_message(results, discovery_ok)
            telegram_send(token, chat_id, message)
            print("Telegram status sent.")
            return 0
        finally:
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
