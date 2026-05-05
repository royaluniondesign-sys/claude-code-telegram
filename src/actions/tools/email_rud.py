"""RUD Studio email tools — IMAP/SMTP via IONOS for hello@royaluniondesign.com.

Auto-discovered by AURA MCP server.
FROM address: hello@royaluniondesign.com (professional client-facing)
Setup: add IONOS_EMAIL_PASS to .env
"""

from __future__ import annotations

from src.actions.registry import aura_tool
from src.integrations import ionos_email_client as ionos


@aura_tool(
    name="rud_email_list_unread",
    description="Lista emails no leídos en hello@royaluniondesign.com (bandeja de entrada RUD Studio). Devuelve UID, remitente, asunto, snippet.",
    category="email",
    parameters={
        "max_results": {"type": "int", "description": "Máximo de emails a devolver (default 10)"},
    },
)
async def rud_email_list_unread(max_results: int = 10) -> str:
    if not ionos.is_configured():
        return "⚠️ Falta IONOS_EMAIL_PASS en .env. Añádelo y reinicia AURA."
    try:
        msgs = await ionos.list_unread(max_results=max_results)
        if not msgs:
            return "📭 Sin emails no leídos en hello@royaluniondesign.com"
        lines = [f"📬 {len(msgs)} no leído(s) en hello@royaluniondesign.com:\n"]
        for m in msgs:
            lines.append(f"📧 UID: `{m['uid']}`")
            lines.append(f"   De: {m['from']}")
            lines.append(f"   Asunto: {m['subject']}")
            lines.append(f"   Fecha: {m['date']}")
            lines.append(f"   {m['snippet']}...")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error IMAP: {e}"


@aura_tool(
    name="rud_email_read",
    description="Lee el contenido completo de un email por UID. Usa rud_email_list_unread primero para obtener el UID.",
    category="email",
    parameters={
        "uid": {"type": "str", "description": "UID del mensaje (de rud_email_list_unread)"},
        "mark_read": {"type": "bool", "description": "Marcar como leído (default True)"},
    },
)
async def rud_email_read(uid: str, mark_read: bool = True) -> str:
    if not ionos.is_configured():
        return "⚠️ Falta IONOS_EMAIL_PASS en .env."
    try:
        msg = await ionos.get_message(uid)
        if mark_read:
            await ionos.mark_read(uid)
        return (
            f"📧 **De:** {msg['from']}\n"
            f"**Para:** {msg['to']}\n"
            f"**Asunto:** {msg['subject']}\n"
            f"**Fecha:** {msg['date']}\n"
            f"**Message-ID:** {msg['message_id']}\n\n"
            f"---\n{msg['body']}"
        )
    except Exception as e:
        return f"❌ Error: {e}"


@aura_tool(
    name="rud_email_search",
    description="Busca emails en hello@royaluniondesign.com. Query: 'FROM:cliente@x.com', 'SUBJECT:presupuesto', 'BODY:urgente'",
    category="email",
    parameters={
        "query": {"type": "str", "description": "Query de búsqueda (FROM:, SUBJECT:, BODY:, o texto libre)"},
        "max_results": {"type": "int", "description": "Máximo resultados (default 10)"},
    },
)
async def rud_email_search(query: str, max_results: int = 10) -> str:
    if not ionos.is_configured():
        return "⚠️ Falta IONOS_EMAIL_PASS en .env."
    try:
        msgs = await ionos.search(query=query, max_results=max_results)
        if not msgs:
            return f"📭 Sin resultados para: {query}"
        lines = [f"🔍 {len(msgs)} resultado(s) para `{query}`:\n"]
        for m in msgs:
            lines.append(f"📧 UID: `{m['uid']}`  |  De: {m['from']}")
            lines.append(f"   {m['subject']}  |  {m['date']}")
            lines.append(f"   {m['snippet']}...")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {e}"


@aura_tool(
    name="rud_email_send",
    description=(
        "Envía un email DESDE hello@royaluniondesign.com a cualquier destinatario. "
        "Para respuestas a clientes, presupuestos y propuestas de RUD Studio."
    ),
    category="email",
    parameters={
        "to": {"type": "str", "description": "Email del destinatario"},
        "subject": {"type": "str", "description": "Asunto"},
        "body": {"type": "str", "description": "Cuerpo del email en texto plano"},
        "html": {"type": "str", "description": "Cuerpo HTML (opcional, para emails más elaborados)"},
    },
)
async def rud_email_send(to: str, subject: str, body: str, html: str = "") -> str:
    if not ionos.is_configured():
        return "⚠️ Falta IONOS_EMAIL_PASS en .env."
    try:
        result = await ionos.send(to=to, subject=subject, body=body, html=html or None)
        if result.get("ok"):
            return f"✅ Email enviado a {to} desde hello@royaluniondesign.com"
        return f"❌ Error SMTP: {result.get('error')}"
    except Exception as e:
        return f"❌ Error: {e}"


@aura_tool(
    name="rud_email_reply",
    description="Responde a un email existente manteniendo el hilo. Necesitas el UID del mensaje original.",
    category="email",
    parameters={
        "uid": {"type": "str", "description": "UID del mensaje al que respondes"},
        "body": {"type": "str", "description": "Texto de la respuesta"},
        "html": {"type": "str", "description": "HTML (opcional)"},
    },
)
async def rud_email_reply(uid: str, body: str, html: str = "") -> str:
    if not ionos.is_configured():
        return "⚠️ Falta IONOS_EMAIL_PASS en .env."
    try:
        original = await ionos.get_message(uid)
        to = original.get("from", "")
        subject = original.get("subject", "")
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"
        msg_id = original.get("message_id", "")
        result = await ionos.send(
            to=to,
            subject=subject,
            body=body,
            html=html or None,
            reply_to_message_id=msg_id,
        )
        if result.get("ok"):
            return f"✅ Respuesta enviada a {to}"
        return f"❌ Error: {result.get('error')}"
    except Exception as e:
        return f"❌ Error: {e}"


@aura_tool(
    name="rud_email_status",
    description="Verifica si el email IONOS está configurado y la conexión funciona.",
    category="email",
    parameters={},
)
async def rud_email_status() -> str:
    if not ionos.is_configured():
        return (
            "⚠️ Email RUD no configurado.\n\n"
            "Añade en .env:\n"
            "IONOS_EMAIL_USER=hello@royaluniondesign.com\n"
            "IONOS_EMAIL_PASS=tu_password_ionos\n\n"
            "Luego reinicia AURA: launchctl kickstart -k gui/$(id -u)/com.aura.telegram-bot"
        )
    # Test connection
    try:
        import imaplib
        with imaplib.IMAP4_SSL(ionos.IMAP_HOST, ionos.IMAP_PORT) as imap:
            imap.login(ionos._EMAIL_USER, ionos._get_pass())
        return f"✅ Email IONOS activo → {ionos._EMAIL_USER}"
    except Exception as e:
        return f"❌ Conexión IMAP fallida: {e}"
