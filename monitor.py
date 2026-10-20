#!/usr/bin/env python3
"""Monitor new products on syokugan-ohkoku.com and send a notification."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import smtplib
import ssl
import sys
import threading
import time
from dataclasses import dataclass, asdict
from email.message import EmailMessage
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


SITE_URL = "https://syokugan-ohkoku.com/index.php"
DEFAULT_STATE_FILE = Path(__file__).parent / "data" / "state.json"
DEFAULT_SECTIONS = ("hc1", "hc3", "hc7")
RESTOCK_SECTION = "hc7"
SOURCE_MODE = "full_lists_v1"
MIGRATION_ALERT_CODES = frozenset({"2608231", "2608233"})
SECTION_LIST_IDS = {
    "hc1": 1,
    "hc3": 3,
    "hc4": 4,
    "hc5": 5,
    "hc6": 6,
    "hc7": 7,
    "hc9": 9,
}
SECTION_NAMES = {
    "hc1": "新食玩信息",
    "hc3": "先行预约",
    "hc4": "最新到货",
    "hc5": "即将到货",
    "hc7": "再入荷",
}
CODE_RE = re.compile(r"(?:\?|&)code=([A-Za-z0-9_-]+)")
IMAGE_BUDGET_SECONDS = 15
IMAGE_TIMEOUT_SECONDS = 5
MAX_IMAGE_BYTES = 1_000_000
MAX_EMBEDDED_IMAGE_BYTES = 5_000_000


def redact_sensitive(text: str) -> str:
    sensitive_values = {
        os.environ.get(name, "").strip()
        for name in ("SMTP_USERNAME", "SMTP_AUTH_CODE", "EMAIL_TO")
    }
    patterns = [
        re.escape(value)
        for value in sorted(sensitive_values, key=len, reverse=True)
        if value
    ]
    patterns.append(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+")
    return re.sub("|".join(patterns), "[REDACTED]", text)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        super().error(redact_sensitive(message))


@dataclass
class Product:
    code: str
    title: str
    release_date: str
    url: str
    section: str
    image_url: str = ""


class ProductParser(HTMLParser):
    """Extract product cards from a homepage section or its full list page."""

    def __init__(
        self, sections: Iterable[str], *, fixed_section: str | None = None
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.sections = set(sections)
        self.fixed_section = fixed_section
        self.active_section = fixed_section
        self.card_depth = 0
        self.current: dict[str, str] | None = None
        self.capture_title = False
        self.capture_bold = False
        self.products: dict[str, Product] = {}
        self.div_depth = 0
        self.content_depth: int | None = None
        self.content_lists = 0
        self.content_closed = False
        self.placeholder_count = 0
        self.card_count = 0
        self.empty_list_text = False
        self.invalid_structure = False
        self.document_tags: list[str] = []
        self.document_closed: set[str] = set()

    @staticmethod
    def _classes(attrs: dict[str, str | None]) -> set[str]:
        return set((attrs.get("class") or "").split())

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        classes = self._classes(attrs)
        if tag in ("html", "body"):
            expected = [] if tag == "html" else ["html"]
            if self.document_tags != expected or tag in self.document_closed:
                self.invalid_structure = True
            self.document_tags.append(tag)
        if tag == "div":
            self.div_depth += 1
            if "content-line" in classes:
                if self.content_depth is not None or (
                    self.fixed_section and self.document_tags != ["html", "body"]
                ):
                    self.invalid_structure = True
                self.content_lists += 1
                self.content_depth = self.div_depth

        if tag == "h1" and self.fixed_section is None:
            section_id = attrs.get("id")
            self.active_section = section_id if section_id in self.sections else None
            return

        is_product_card = any(
            class_name == "item-wrap" or (
                class_name.startswith("item-wrap-") and class_name != "item-wrap-dammy"
            )
            for class_name in classes
        )
        if tag == "div" and "item-wrap-dammy" in classes:
            if self.content_depth is not None and not self.current:
                self.placeholder_count += 1
        if self.active_section and tag == "div" and is_product_card:
            if self.current:
                self.invalid_structure = True
                self.card_depth += 1
                return
            if self.fixed_section and (
                self.content_depth is None or "body" not in self.document_tags
            ):
                self.invalid_structure = True
            self.card_count += 1
            self.card_depth = 1
            self.current = {
                "code": "",
                "title": "",
                "release_date": "",
                "url": "",
                "section": self.active_section,
                "image_url": "",
            }
            return

        if not self.current:
            return

        if tag == "div":
            self.card_depth += 1
        elif tag == "a":
            href = attrs.get("href") or ""
            match = CODE_RE.search(href)
            if match and not self.current["code"]:
                self.current["code"] = match.group(1)
                self.current["url"] = urljoin(SITE_URL, href)
        elif tag == "img" and "item-icon" in classes:
            image_src = attrs.get("src") or ""
            if image_src and not self.current["image_url"]:
                self.current["image_url"] = urljoin(SITE_URL, image_src)
        elif tag == "span" and "item-webtitle" in classes:
            self.capture_title = True
        elif tag == "b":
            self.capture_bold = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("html", "body"):
            if (
                not self.document_tags or self.document_tags[-1] != tag
                or self.div_depth != 0
            ):
                self.invalid_structure = True
            else:
                self.document_tags.pop()
            self.document_closed.add(tag)
        if tag == "div":
            if self.content_depth == self.div_depth:
                self.content_depth = None
                self.content_closed = True
            self.div_depth -= 1
            if self.div_depth < 0:
                self.invalid_structure = True
        if not self.current:
            return

        if tag == "span" and self.capture_title:
            self.capture_title = False
        elif tag == "b" and self.capture_bold:
            self.capture_bold = False
        elif tag == "div":
            self.card_depth -= 1
            if self.card_depth == 0:
                code = self.current["code"]
                if code and self.current["title"]:
                    product = Product(**self.current)
                    self.products.setdefault(
                        f"{product.section}:{product.code}", product
                    )
                else:
                    self.invalid_structure = True
                self.current = None
                self.capture_title = False
                self.capture_bold = False

    def handle_data(self, data: str) -> None:
        if not self.current:
            if self.content_depth is not None and data.strip():
                self.empty_list_text = True
            return
        text = " ".join(data.split())
        if not text:
            return
        if self.capture_title:
            self.current["title"] = " ".join(
                part for part in (self.current["title"], text) if part
            )
        elif self.capture_bold and not self.current["release_date"]:
            self.current["release_date"] = text

    def validate_document(self) -> None:
        if (
            self.invalid_structure or self.current or self.div_depth != 0
            or self.document_tags or self.document_closed != {"html", "body"}
            or (self.fixed_section and (
                not self.content_lists or not self.content_closed or self.empty_list_text
                or (not self.card_count and not self.placeholder_count)
            ))
        ):
            raise RuntimeError("商品列表结构不完整或已变化，保留原有基线")


def http_request(
    url: str,
    *,
    data: bytes | None = None,
    content_type: str = "application/x-www-form-urlencoded; charset=utf-8",
    timeout: float = 20,
    attempts: int = 3,
    max_bytes: int | None = None,
) -> bytes:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; SyokuganNewProductMonitor/1.0; "
            "+https://syokugan-ohkoku.com/)"
        )
    }
    if data is not None:
        headers["Content-Type"] = content_type
    request = Request(url, data=data, headers=headers)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = (
                    response.read() if max_bytes is None else response.read(max_bytes + 1)
                )
                if max_bytes is not None and len(payload) > max_bytes:
                    raise ValueError("响应内容超过大小限制")
                return payload
        except (HTTPError, URLError, TimeoutError) as error:
            last_error = error
            if attempt < attempts - 1:
                time.sleep(2**attempt)
    raise RuntimeError(f"请求失败：{url} ({last_error})")


def fetch_products(sections: Iterable[str]) -> dict[str, Product]:
    products: dict[str, Product] = {}
    for section in sections:
        list_id = SECTION_LIST_IDS.get(section)
        if list_id is None:
            page_url = SITE_URL
            parser = ProductParser((section,))
        else:
            page_url = urljoin(SITE_URL, f"list-shohin.php?id={list_id}")
            parser = ProductParser((section,), fixed_section=section)

        html = http_request(page_url).decode("utf-8", errors="replace")
        parser.feed(html)
        parser.close()
        parser.validate_document()
        if not parser.products and section != RESTOCK_SECTION:
            raise RuntimeError(
                f"栏目 {section} 的完整列表解析结果为空，网站结构可能已经变化"
            )
        products.update(parser.products)
    return products


def validate_state(state: object) -> dict:
    error = "状态文件无效或跟踪记录不一致，已停止检查；不会发送邮件或覆盖基线"
    if not isinstance(state, dict) or state.get("initialized") is not True:
        raise RuntimeError(error)
    full_lists = state.get("source_mode") == SOURCE_MODE
    if "source_mode" in state and not full_lists:
        raise RuntimeError(error)
    for name in ("known_codes", "active_restock_codes", "monitored_sections"):
        if name not in state:
            if name == "monitored_sections" or (
                not full_lists and name == "active_restock_codes"
            ):
                continue
            raise RuntimeError(error)
        values = state[name]
        pattern = r"hc[0-9]+" if name == "monitored_sections" else r"[A-Za-z0-9_-]+"
        if (
            not isinstance(values, list)
            or any(
                not isinstance(value, str) or not re.fullmatch(pattern, value)
                for value in values
            )
            or len(set(values)) != len(values)
        ):
            raise RuntimeError(error)
    snapshots = state.get("last_snapshot")
    if not isinstance(snapshots, dict):
        raise RuntimeError(error)
    snapshot_new_codes: set[str] = set()
    snapshot_restock_codes: set[str] = set()
    snapshot_sections: set[str] = set()
    for key, product in snapshots.items():
        if not isinstance(product, dict):
            raise RuntimeError(error)
        if any(
            not isinstance(product.get(name), str)
            for name in ("code", "title", "release_date", "url", "section")
        ) or ("image_url" in product and not isinstance(product["image_url"], str)):
            raise RuntimeError(error)
        code, section = product["code"], product["section"]
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+", code)
            or not re.fullmatch(r"hc[0-9]+", section)
            or key != f"{section}:{code}"
            or not product["title"].strip()
            or not product["url"].strip()
        ):
            raise RuntimeError(error)
        snapshot_sections.add(section)
        if section == RESTOCK_SECTION:
            snapshot_restock_codes.add(code)
        else:
            snapshot_new_codes.add(code)
    # Legacy homepage baselines intentionally lack full-list tracking coverage.
    if full_lists and (
        not snapshot_new_codes.issubset(state["known_codes"])
        or snapshot_restock_codes != set(state["active_restock_codes"])
    ):
        raise RuntimeError(error)
    if state.get("monitored_sections") and not snapshot_sections.issubset(
        state["monitored_sections"]
    ):
        raise RuntimeError(error)
    return state


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"initialized": False, "known_codes": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"无法读取状态文件 {path}: {error}") from error
    return validate_state(state)


def save_state(
    path: Path,
    products: dict[str, Product],
    *,
    known_codes: set[str],
    active_restock_codes: set[str],
    monitored_sections: Iterable[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "initialized": True,
        "source_mode": SOURCE_MODE,
        "known_codes": sorted(known_codes),
        "active_restock_codes": sorted(active_restock_codes),
        "monitored_sections": sorted(set(monitored_sections)),
        "last_snapshot": {
            key: asdict(product) for key, product in sorted(products.items())
        },
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def format_notification(products: list[Product]) -> tuple[str, str]:
    contains_restock = any(
        product.section == RESTOCK_SECTION for product in products
    )
    contains_new = any(product.section != RESTOCK_SECTION for product in products)
    if contains_restock and contains_new:
        notification_type = "上新/补货"
    elif contains_restock:
        notification_type = "补货"
    else:
        notification_type = "上新"
    title = f"食玩王国发现 {len(products)} 件{notification_type}"
    lines: list[str] = []
    for index, product in enumerate(products, start=1):
        section_name = SECTION_NAMES.get(product.section, product.section)
        lines.extend(
            [
                f"{index}. {product.title}",
                f"栏目：{section_name}",
                f"发售：{product.release_date or '未标明'}",
                f"链接：{product.url}",
                "",
            ]
        )
    return title, "\n".join(lines).strip()


def build_html_notification(
    title: str, products: list[Product], image_cids: dict[str, str]
) -> str:
    cards: list[str] = []
    for product in products:
        section_name = SECTION_NAMES.get(product.section, product.section)
        cid = image_cids.get(f"{product.section}:{product.code}")
        image_cell = ""
        if cid:
            image_cell = (
                '<td width="170" valign="top" style="padding:0 16px 0 0">'
                f'<a href="{escape(product.url, quote=True)}">'
                f'<img src="cid:{escape(cid, quote=True)}" width="150" '
                f'alt="{escape(product.title, quote=True)}" '
                'style="display:block;width:150px;height:auto;border:0">'
                "</a></td>"
            )
        cards.append(
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="margin:0 0 18px;border-bottom:1px solid #dddddd;padding-bottom:18px">'
            f"<tr>{image_cell}<td valign=\"top\">"
            f'<div style="font-size:16px;font-weight:bold;line-height:1.5">'
            f"{escape(product.title)}</div>"
            f'<div style="margin-top:8px;color:#555555">栏目：{escape(section_name)}</div>'
            f'<div style="margin-top:4px;color:#555555">发售：'
            f"{escape(product.release_date or '未标明')}</div>"
            f'<div style="margin-top:10px"><a href="{escape(product.url, quote=True)}">'
            "查看商品详情</a></div></td></tr></table>"
        )

    return (
        '<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;'
        'color:#222222;line-height:1.5">'
        f'<div style="max-width:680px;margin:0 auto"><h2>{escape(title)}</h2>'
        f"{''.join(cards)}</div></body></html>"
    )


def download_notification_images(
    products: list[Product],
) -> list[tuple[int, bytes, str, str]]:
    deadline = time.monotonic() + IMAGE_BUDGET_SECONDS
    results: queue.Queue = queue.Queue()
    stop = threading.Event()

    def download() -> None:
        total_bytes = 0
        for index, product in enumerate(products):
            remaining = deadline - time.monotonic()
            if stop.is_set() or remaining <= 0:
                break
            if not product.image_url:
                continue
            try:
                mime_type = mimetypes.guess_type(product.image_url)[0] or "image/png"
                maintype, subtype = mime_type.split("/", 1)
                if maintype != "image":
                    raise ValueError("不是图片类型")
                image_data = http_request(
                    product.image_url,
                    timeout=min(IMAGE_TIMEOUT_SECONDS, remaining),
                    attempts=1,
                    max_bytes=min(MAX_IMAGE_BYTES, MAX_EMBEDDED_IMAGE_BYTES - total_bytes),
                )
                if stop.is_set() or time.monotonic() >= deadline:
                    break
                filename = Path(product.image_url.split("?", 1)[0]).name
                total_bytes += len(image_data)
                results.put((index, image_data, subtype, filename))
                if total_bytes >= MAX_EMBEDDED_IMAGE_BYTES:
                    break
            except Exception:
                results.put(None)

    # DNS, redirects and slow trickle reads need not obey a socket timeout.
    # This worker owns no SMTP/state objects and cannot delay process exit.
    worker = threading.Thread(target=download, daemon=True, name="optional-images")
    worker.start()
    worker.join(max(0, deadline - time.monotonic()))
    stop.set()
    images = []
    failed = worker.is_alive()
    while True:
        try:
            result = results.get_nowait()
        except queue.Empty:
            break
        if result is None:
            failed = True
        else:
            images.append(result)
    if failed:
        print("部分商品图片不可用或下载超时，继续发送文字内容")
    return images


def send_notification(
    title: str, body: str, products: list[Product] | None = None
) -> None:
    smtp_username = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_auth_code = os.environ.get("SMTP_AUTH_CODE", "").strip()
    email_to = os.environ.get("EMAIL_TO", "").strip()
    email_settings = (smtp_username, smtp_auth_code, email_to)
    if not all(email_settings):
        raise RuntimeError(
            "邮件配置不完整，需要同时配置 SMTP_USERNAME、SMTP_AUTH_CODE 和 EMAIL_TO"
        )

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = smtp_username
    message["To"] = email_to
    message.set_content(body)

    embedded_images: list[tuple[str, bytes, str, str]] = []
    image_cids: dict[str, str] = {}
    if products:
        for index, image_data, subtype, filename in download_notification_images(products):
            product = products[index]
            cid = f"product-{index}-{product.code}@syokugan-monitor"
            image_cids[f"{product.section}:{product.code}"] = cid
            embedded_images.append((cid, image_data, subtype, filename))

        message.add_alternative(
            build_html_notification(title, products, image_cids), subtype="html"
        )
        html_part = message.get_payload()[-1]
        for cid, image_data, subtype, filename in embedded_images:
            html_part.add_related(
                image_data,
                maintype="image",
                subtype=subtype,
                cid=f"<{cid}>",
                filename=filename,
            )

    smtp = None
    try:
        smtp = smtplib.SMTP_SSL(
            "smtp.qq.com",
            465,
            context=ssl.create_default_context(),
            timeout=30,
        )
        smtp.login(smtp_username, smtp_auth_code)
        refused_recipients = smtp.send_message(message)
        if refused_recipients:
            raise RuntimeError("邮件发送失败：部分收件人被拒绝，状态未更新")
    except smtplib.SMTPAuthenticationError as error:
        raise RuntimeError(
            "QQ 邮箱认证失败，请检查是否已开启 SMTP 服务以及授权码是否正确"
        ) from error
    finally:
        if smtp is not None:
            # QUIT cannot revoke confirmed DATA acceptance or mask a send error.
            try:
                code, _ = smtp.quit()
                if code != 221:
                    print("SMTP 连接退出异常；保留邮件发送阶段的结果")
            except Exception:
                print("SMTP 连接退出异常；保留邮件发送阶段的结果")
            finally:
                try:
                    smtp.close()
                except Exception:
                    print("SMTP 连接清理异常；保留邮件发送阶段的结果")

    print("通知邮件已发送")


def parse_args(argv=None) -> argparse.Namespace:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅检查商品，不发送邮件或写入状态",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="发送一封带示例商品图的测试邮件，不修改状态",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.test_notification:
        sample_products = list(fetch_products(DEFAULT_SECTIONS).values())[:1]
        if args.dry_run:
            print(f"只读检查：已识别 {len(sample_products)} 件示例商品，不发送测试邮件")
            return 0
        _, sample_body = format_notification(sample_products)
        send_notification(
            "食玩王国图片邮件测试",
            "图片邮件配置成功。以下为当前商品示例：\n\n" + sample_body,
            sample_products,
        )
        return 0

    state_file = Path(os.environ.get("STATE_FILE", DEFAULT_STATE_FILE))
    sections = tuple(
        section.strip()
        for section in os.environ.get(
            "MONITOR_SECTIONS", ",".join(DEFAULT_SECTIONS)
        ).split(",")
        if section.strip()
    )

    state = load_state(state_file)
    products = fetch_products(sections)
    known_codes = set(state.get("known_codes", []))
    monitored_sections = set(state.get("monitored_sections", []))
    if not monitored_sections:
        monitored_sections = {
            snapshot.get("section", "")
            for snapshot in state.get("last_snapshot", {}).values()
            if snapshot.get("section")
        }
    current_restock_codes = {
        product.code
        for product in products.values()
        if product.section == RESTOCK_SECTION
    }
    previous_restock_codes = set(state.get("active_restock_codes", []))
    migrating_to_full_lists = state.get("source_mode") != SOURCE_MODE

    if not state.get("initialized"):
        if args.dry_run:
            print(f"只读检查：已识别 {len(products)} 件现有商品，不发送邮件或写入状态")
            return 0
        initial_known_codes = {
            product.code
            for product in products.values()
            if product.section != RESTOCK_SECTION
        }
        save_state(
            state_file,
            products,
            known_codes=initial_known_codes,
            active_restock_codes=current_restock_codes,
            monitored_sections=sections,
        )
        print(f"首次运行：已记录 {len(products)} 件现有商品，不发送历史商品通知")
        return 0

    restock_section_was_monitored = RESTOCK_SECTION in monitored_sections
    new_products: list[Product] = []
    for product in products.values():
        if migrating_to_full_lists:
            if (
                product.code in MIGRATION_ALERT_CODES
                and product.code not in known_codes
            ):
                new_products.append(product)
        elif product.section == RESTOCK_SECTION:
            if (
                restock_section_was_monitored
                and product.code not in previous_restock_codes
            ):
                new_products.append(product)
        elif product.code not in known_codes:
            new_products.append(product)
    new_products.sort(key=lambda product: (product.section, product.code), reverse=True)

    if args.dry_run:
        print(
            f"只读检查：发现 {len(new_products)} 件上新或补货；"
            f"当前共识别 {len(products)} 件商品，不发送邮件或写入状态"
        )
        return 0

    if new_products:
        title, body = format_notification(new_products)
        send_notification(title, body, new_products)
        print(f"发现 {len(new_products)} 件上新或补货")
    else:
        print(f"没有上新或补货；当前页面共识别 {len(products)} 件商品")

    known_codes.update(
        product.code
        for product in products.values()
        if product.section != RESTOCK_SECTION
    )
    save_state(
        state_file,
        products,
        known_codes=known_codes,
        active_restock_codes=current_restock_codes,
        monitored_sections=sections,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(redact_sensitive(f"错误：{error}"), file=sys.stderr)
        raise SystemExit(1)
