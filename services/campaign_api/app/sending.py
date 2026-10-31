import os
import smtplib
import html
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from urllib.parse import urlsplit
from uuid import UUID


class _HTMLTextExtractor(HTMLParser):
    """Convert HTML content into a readable plain-text alternative."""

    BLOCK_TAGS = {
        "address", "article", "blockquote", "br", "div", "h1", "h2",
        "h3", "h4", "h5", "h6", "li", "p", "section", "tr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html_body: str) -> str:
    """Produce a simple plain-text alternative from HTML."""
    parser = _HTMLTextExtractor()
    parser.feed(html_body)
    parser.close()

    lines = [
        line.strip()
        for line in "".join(parser.parts).splitlines()
        if line.strip()
    ]

    return "\n".join(lines) or "Please view this message in an HTML-capable email client."


def validate_tracking_base_url(value: str) -> str:
    """Validate the tracking service base URL."""
    value = value.strip()

    if not value or value != value.strip():
        raise ValueError("TRACK_BASE_URL must be configured without surrounding whitespace")

    parsed = urlsplit(value)

    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("TRACK_BASE_URL must contain a valid hostname and no credentials")

    if parsed.query or parsed.fragment:
        raise ValueError("TRACK_BASE_URL must not contain a query or fragment")

    local_hosts = {"127.0.0.1", "localhost", "::1"}

    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and parsed.hostname in local_hosts:
        pass
    else:
        raise ValueError(
            "TRACK_BASE_URL must use HTTPS, except for local loopback testing"
        )

    return value.rstrip("/")


class _TrackingHTMLRewriter(HTMLParser):
    """Rewrite only approved anchor links and optionally insert a pixel."""

    def __init__(self, link_map: dict[str, str], pixel_html: str | None):
        super().__init__(convert_charrefs=False)
        self.link_map = link_map
        self.pixel_html = pixel_html
        self.pixel_inserted = False
        self.parts = []

    def _render_start_tag(self, tag, attrs, self_closing=False):
        if tag.lower() == "a":
            updated_attrs = []

            for name, value in attrs:
                if name.lower() == "href" and value in self.link_map:
                    value = self.link_map[value]
                updated_attrs.append((name, value))

            attrs = updated_attrs

        rendered_attrs = []

        for name, value in attrs:
            if value is None:
                rendered_attrs.append(name)
            else:
                rendered_attrs.append(
                    f'{name}="{html.escape(value, quote=True)}"'
                )

        ending = " />" if self_closing else ">"
        self.parts.append(f"<{tag} {' '.join(rendered_attrs)}{ending}"
                          if rendered_attrs else f"<{tag}{ending}")

    def handle_starttag(self, tag, attrs):
        self._render_start_tag(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._render_start_tag(tag, attrs, self_closing=True)

    def handle_endtag(self, tag):
        if (
            tag.lower() == "body"
            and self.pixel_html
            and not self.pixel_inserted
        ):
            self.parts.append(self.pixel_html)
            self.pixel_inserted = True

        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def unknown_decl(self, data):
        self.parts.append(f"<![{data}]>")


def add_tracking_to_html(
    html_body: str,
    token: str,
    tracking_base_url: str,
    approved_links: list[dict],
) -> str:
    """Rewrite approved links and add a per-message open pixel."""
    base_url = validate_tracking_base_url(tracking_base_url)

    link_map = {}

    for link in approved_links:
        destination = link["destination_url"]
        link_no = int(link["link_no"])

        # Only destinations explicitly approved and stored in the DB
        # are eligible for rewriting.
        link_map.setdefault(
            destination,
            f"{base_url}/c/{token}/{link_no}",
        )

    pixel_url = f"{base_url}/o/{token}.gif"

    pixel_html = (
        f'<img src="{html.escape(pixel_url, quote=True)}" '
        'width="1" height="1" alt="" '
        'style="display:none;width:1px;height:1px;" />'
    )

    parser = _TrackingHTMLRewriter(link_map, pixel_html)
    parser.feed(html_body)
    parser.close()

    if not parser.pixel_inserted:
        parser.parts.append(pixel_html)

    return "".join(parser.parts)


def build_email(
    *,
    campaign_id: UUID,
    message_id: UUID,
    recipient_email: str,
    from_email: str,
    subject: str,
    html_body: str,
    token: str,
    tracking_enabled: bool,
    approved_links: list[dict],
    tracking_base_url: str | None,
) -> EmailMessage:
    """Build a multipart email, optionally adding open and click tracking."""
    final_html = html_body

    if tracking_enabled:
        if not token:
            raise ValueError("A message tracking token is required")

        if not tracking_base_url:
            raise ValueError("TRACK_BASE_URL is required for tracking-enabled campaigns")

        final_html = add_tracking_to_html(
            html_body=html_body,
            token=token,
            tracking_base_url=tracking_base_url,
            approved_links=approved_links,
        )

    message = EmailMessage()

    message["From"] = from_email
    message["To"] = recipient_email
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["X-ESP-Message-Id"] = str(message_id)
    message["X-ESP-Campaign-Id"] = str(campaign_id)
    message["Message-ID"] = make_msgid(domain="sunshine.optimoze.com")

    message.set_content(html_to_text(html_body))
    message.add_alternative(final_html, subtype="html")

    return message


def send_email(message: EmailMessage) -> None:
    """Submit one email to the configured SMTP server."""
    host = os.environ.get("SMTP_HOST")
    port_value = os.environ.get("SMTP_PORT")

    if not host or not port_value:
        raise RuntimeError("SMTP_HOST and SMTP_PORT must be configured")

    try:
        port = int(port_value)
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be a valid integer") from exc

    with smtplib.SMTP(host=host, port=port, timeout=10) as smtp:
        smtp.send_message(message)
