from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

CATEGORY_URL = os.getenv(
    "HERMES_CATEGORY_URL",
    "https://www.hermes.com/br/pt/category/artigos-de-couro/bolsas-e-bolsas-clutch/bolsas-e-bolsas-clutch-femininas/?facet_line=picotin_lock",
)
DIRECT_URLS = [u.strip() for u in os.getenv("HERMES_DIRECT_URLS", "").splitlines() if u.strip()]
TARGET_RE = re.compile(r"\bpicotin\s+lock\s*18\b", re.I)
PRICE_RE = re.compile(r"R\$\s*[\d\.]+(?:,\d{2})?")

# Three fresh browser contexts per product. We require unanimity (3/3) to classify a state.
CHECKS_PER_PRODUCT = 3
MIN_AVAILABLE_AGREE = 3
MIN_UNAVAILABLE_AGREE = 3
BETWEEN_CHECKS_SECONDS = max(0.5, float(os.getenv("BETWEEN_CHECKS_SECONDS", "1.5")))
NAV_TIMEOUT_MS = max(15000, int(os.getenv("NAV_TIMEOUT_MS", "60000")))

POSITIVE_CTA_PATTERNS = [
    r"adicionar\s+à\s+sacola",
    r"adicionar\s+ao\s+carrinho",
    r"add\s+to\s+bag",
    r"add\s+to\s+cart",
]

NEGATIVE_MARKERS = [
    "infelizmente, este produto não está mais disponível",
    "este produto não está mais disponível",
    "não está disponível",
    "fora de estoque",
    "esgotado",
    "sem estoque",
    "não disponível em estoque",
    "no stock",
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
    "verifique se você é um humano",
    "access denied",
    "forbidden",
    "temporarily unavailable",
    "just a moment",
]


@dataclass
class CheckEvidence:
    url: str
    title: str
    price: str | None
    status: str
    product_match: bool
    cta_visible_enabled: bool
    cta_text: str | None
    jsonld_instock: bool
    jsonld_outofstock: bool
    negative_text: bool
    waiting_text: bool
    blocked_page: bool
    score_positive: int
    score_negative: int
    reasons: list[str]


@dataclass
class ProductResult:
    url: str
    title: str
    price: str | None
    status: str
    confidence: str
    checks: list[CheckEvidence]


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def extract_jsonld_availability(scripts: list[str]) -> list[str]:
    values: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                k = str(key).lower()
                if k in {"availability", "itemavailability", "availabilitystatus"}:
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


async def first_visible_enabled_cta(page: Page) -> tuple[bool, str | None]:
    pattern = re.compile("|".join(POSITIVE_CTA_PATTERNS), re.I)
    candidates = page.locator("button, a, [role='button']").filter(has_text=pattern)
    count = await candidates.count()

    for i in range(count):
        element = candidates.nth(i)
        try:
            if not await element.is_visible():
                continue
            disabled_attr = await element.get_attribute("disabled")
            aria_disabled = (await element.get_attribute("aria-disabled") or "").lower()
            data_disabled = (await element.get_attribute("data-disabled") or "").lower()
            if disabled_attr is not None or aria_disabled == "true" or data_disabled == "true":
                continue
            text = clean_text(await element.inner_text())
            return True, text or None
        except Exception:
            continue
    return False, None


async def open_fresh_page(browser: Browser, url: str, cache_buster: int) -> tuple[Any, Page]:
    context = await browser.new_context(
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        viewport={"width": 1440, "height": 1200},
        color_scheme="light",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    check_url = url + ("&" if "?" in url else "?") + f"_stockcheck={cache_buster}"
    await page.goto(check_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    # Give storefront JS time to hydrate the stock controls.
    await page.wait_for_timeout(2500)
    return context, page


async def check_once(browser: Browser, url: str, check_no: int) -> CheckEvidence:
    context = None
    try:
        context, page = await open_fresh_page(browser, url, int(time.time() * 1000) + check_no)

        body_text_raw = await page.locator("body").inner_text(timeout=20000)
        body_text = clean_text(body_text_raw)

        try:
            h1_text = clean_text(await page.locator("h1").first.inner_text(timeout=5000))
        except Exception:
            h1_text = ""

        title = h1_text or clean_text(await page.title())
        price_match = PRICE_RE.search(body_text_raw)
        price = price_match.group(0) if price_match else None

        jsonld_scripts = await page.locator('script[type="application/ld+json"]').all_inner_texts()
        availability_values = [clean_text(x) for x in extract_jsonld_availability(jsonld_scripts)]
        jsonld_instock = any("instock" in x and "outofstock" not in x for x in availability_values)
        jsonld_outofstock = any("outofstock" in x or "soldout" in x for x in availability_values)

        cta_visible_enabled, cta_text = await first_visible_enabled_cta(page)
        negative_text = any(marker in body_text for marker in NEGATIVE_MARKERS)
        waiting_text = any(marker in body_text for marker in WAITING_MARKERS)
        blocked_page = any(marker in body_text for marker in BLOCK_MARKERS)

        product_match = bool(TARGET_RE.search(title) or TARGET_RE.search(body_text[:16000]))

        # Strong evidence is intentionally redundant. One signal alone never proves stock.
        score_positive = (2 if cta_visible_enabled else 0) + (2 if jsonld_instock else 0)
        score_negative = (3 if negative_text else 0) + (3 if jsonld_outofstock else 0) + (2 if waiting_text else 0)

        # AVAILABLE requires: correct product, a real enabled purchase CTA, no contradictory
        # text/data, and at least one independent positive signal besides product matching.
        available = (
            product_match
            and cta_visible_enabled
            and score_positive >= 3
            and score_negative == 0
            and not blocked_page
        )

        # UNAVAILABLE is only accepted with explicit negative evidence AND no positive CTA.
        unavailable = (
            product_match
            and not cta_visible_enabled
            and score_negative >= 3
            and not blocked_page
        )

        status = "AVAILABLE" if available else "UNAVAILABLE" if unavailable else "UNKNOWN"

        reasons = [
            f"product_match={product_match}",
            f"cta_visible_enabled={cta_visible_enabled}",
            f"cta_text={cta_text!r}",
            f"jsonld_instock={jsonld_instock}",
            f"jsonld_outofstock={jsonld_outofstock}",
            f"negative_text={negative_text}",
            f"waiting_text={waiting_text}",
            f"blocked_page={blocked_page}",
            f"positive_score={score_positive}",
            f"negative_score={score_negative}",
            f"status={status}",
        ]

        return CheckEvidence(
            url=normalize_url(url),
            title=title,
            price=price,
            status=status,
            product_match=product_match,
            cta_visible_enabled=cta_visible_enabled,
            cta_text=cta_text,
            jsonld_instock=jsonld_instock,
            jsonld_outofstock=jsonld_outofstock,
            negative_text=negative_text,
            waiting_text=waiting_text,
            blocked_page=blocked_page,
            score_positive=score_positive,
            score_negative=score_negative,
            reasons=reasons,
        )
    except (PlaywrightTimeoutError, Exception) as exc:
        return CheckEvidence(
            url=normalize_url(url),
            title="",
            price=None,
            status="UNKNOWN",
            product_match=False,
            cta_visible_enabled=False,
            cta_text=None,
            jsonld_instock=False,
            jsonld_outofstock=False,
            negative_text=False,
            waiting_text=False,
            blocked_page=False,
            score_positive=0,
            score_negative=0,
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

    available_count = sum(c.status == "AVAILABLE" for c in checks)
    unavailable_count = sum(c.status == "UNAVAILABLE" for c in checks)

    if available_count >= MIN_AVAILABLE_AGREE:
        final_status = "AVAILABLE"
    elif unavailable_count >= MIN_UNAVAILABLE_AGREE:
        final_status = "UNAVAILABLE"
    else:
        final_status = "UNKNOWN"

    confidence = (
        f"HIGH_{available_count}/{CHECKS_PER_PRODUCT}"
        if final_status == "AVAILABLE"
        else f"HIGH_{unavailable_count}/{CHECKS_PER_PRODUCT}"
        if final_status == "UNAVAILABLE"
        else f"UNCONFIRMED_A{available_count}_U{unavailable_count}"
    )

    return ProductResult(
        url=normalize_url(url),
        title=next((c.title for c in reversed(checks) if c.title), "Picotin Lock 18"),
        price=next((c.price for c in reversed(checks) if c.price), None),
        status=final_status,
        confidence=confidence,
        checks=checks,
    )


async def discover_product_urls(browser: Browser) -> list[str]:
    context = await browser.new_context(
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        viewport={"width": 1440, "height": 1200},
        color_scheme="light",
    )
    page = await context.new_page()
    try:
        await page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(3000)

        anchors = await page.locator("a[href]").evaluate_all(
            "els => els.map(a => ({href: a.href, text: (a.innerText || '').trim(), aria: a.getAttribute('aria-label') || '', title: a.getAttribute('title') || ''}))"
        )

        found: list[str] = []
        for item in anchors:
            text = clean_text(" ".join(str(item.get(k, "")) for k in ("text", "aria", "title")))
            href = str(item.get("href") or "")
            if TARGET_RE.search(text) and "hermes.com" in href:
                found.append(normalize_url(href))

        for url in DIRECT_URLS:
            found.append(normalize_url(url))

        return list(dict.fromkeys(found))
    finally:
        await context.close()


def telegram_send(token: str, chat_id: str, message: str) -> None:
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "false"}
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Telegram HTTP {response.status}")


def build_message(results: list[ProductResult], discovery_ok: bool) -> str:
    now = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime())
    lines = [
        "👜 HERMÈS — PICOTIN LOCK 18",
        f"🕒 Verificado: {now} (São Paulo)",
        f"🔁 Confirmação: {CHECKS_PER_PRODUCT}/3 checagens precisam concordar para concluir",
        "",
    ]

    if not discovery_ok:
        lines += [
            "⚠️ NÃO FOI POSSÍVEL VALIDAR A PÁGINA DA HERMÈS",
            "O monitor não vai interpretar falha de acesso como 'indisponível'.",
            "",
        ]
    elif not results:
        lines += [
            "⚠️ NENHUMA PÁGINA DE PICOTIN LOCK 18 FOI ENCONTRADA",
            "Isso é tratado como NÃO CONFIRMADO, não como 'indisponível'.",
            "",
        ]
    else:
        for item in results:
            if item.status == "AVAILABLE":
                status_line = f"✅ DISPONÍVEL — confirmado ({item.confidence.replace('HIGH_', '')})"
            elif item.status == "UNAVAILABLE":
                status_line = f"❌ INDISPONÍVEL — confirmado ({item.confidence.replace('HIGH_', '')})"
            else:
                status_line = "⚠️ NÃO CONFIRMADO — checagens divergentes/insuficientes"

            lines.append(f"• {item.title}")
            if item.price:
                lines.append(f"  💰 {item.price}")
            lines.append(f"  {status_line}")
            lines.append(f"  🔗 {item.url}")
            lines.append("")

    lines += [
        "🛡️ Disponível só quando há CTA de compra habilitado + sinal de estoque positivo e sem contraditórios, com maioria das checagens concordando.",
        "⏱️ Próxima checagem: aproximadamente 10 min (o GitHub pode atrasar execuções agendadas).",
    ]
    return "\n".join(lines).strip()


async def main() -> int:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            try:
                urls = await discover_product_urls(browser)
                discovery_ok = True
            except Exception as exc:
                print(f"Discovery error: {type(exc).__name__}: {exc}", file=sys.stderr)
                urls = []
                discovery_ok = False

            print(f"Discovered {len(urls)} Picotin Lock 18 URL(s)")

            results: list[ProductResult] = []
            for url in urls:
                result = await inspect_product(browser, url)
                results.append(result)
                print(json.dumps(asdict(result), ensure_ascii=False, indent=2))

            message = build_message(results, discovery_ok)
            telegram_send(bot_token, chat_id, message)
            print("Telegram status sent.")
            return 0
        finally:
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
