import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate
from html.parser import HTMLParser
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


def build_email(
    *,
    campaign_id: UUID,
    message_id: UUID,
    recipient_email: str,
    from_email: str,
    subject: str,
    html_body: str,
) -> EmailMessage:
    """Build a multipart email for one campaign recipient."""
    message = EmailMessage()

    message["From"] = from_email
    message["To"] = recipient_email
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)

    # These headers will later help correlate KumoMTA events
    # with our internal campaign and message records.
    message["X-ESP-Message-Id"] = str(message_id)
    message["X-ESP-Campaign-Id"] = str(campaign_id)

    message.set_content(html_to_text(html_body))
    message.add_alternative(html_body, subtype="html")

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