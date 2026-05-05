"""Gmail MCP tools — auto-discovered by AURA MCP server.

Provides: gmail_list_unread, gmail_read, gmail_search, gmail_send, gmail_reply
All send FROM royaluniondesign@gmail.com once OAuth is configured.
Setup: send /gmail-auth in Telegram.
"""

from __future__ import annotations

from src.actions.registry import aura_tool
from src.integrations import gmail_client


@aura_tool(
    name="gmail_list_unread",
    description="Lista los emails no leídos en royaluniondesign@gmail.com. Devuelve remitente, asunto, snippet y ID.",
    category="email",
    parameters={
        "max_results": {"type": "int", "description": "Máximo de emails a devolver (default 10)"},
        "query": {"type": "str", "description": "Filtro adicional Gmail syntax (ej: 'from:cliente.com' o 'subject:presupuesto')"},
    },
)
async def gmail_list_unread(max_results: int = 10, query: str = "") -> str:
    if not gmail_client.is_configured():
        return "⚠️ Gmail no configurado. Envía /gmail-auth para activarlo."
    try:
        msgs = await gmail_client.list_unread(max_results=max_results, query=query)
        if not msgs:
            return "📭 Sin emails no leídos."
        lines = [f"📬 {len(msgs)} emails no leídos:\n"]
        for m in msgs:
            lines.append(f"📧 ID: `{m['id']}`")
            lines.append(f"   De: {m['from']}")
            lines.append(f"   Asunto: {m['subject']}")
            lines.append(f"   {m['snippet'][:120]}...")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {e}"


@aura_tool(
    name="gmail_read",
    description="Lee el contenido completo de un email por ID. Usa gmail_list_unread primero para obtener el ID.",
    category="email",
    parameters={
        "message_id": {"type": "str", "description": "ID del mensaje de Gmail"},
        "mark_read": {"type": "bool", "description": "Marcar como leído al abrir (default True)"},
    },
)
async def gmail_read(message_id: str, mark_read: bool = True) -> str:
    if not gmail_client.is_configured():
        return "⚠️ Gmail no configurado. Envía /gmail-auth para activarlo."
    try:
        msg = await gmail_client.get_message(message_id)
        if mark_read:
            await gmail_client.mark_read(message_id)
        return (
            f"📧 **De:** {msg['from']}\n"
            f"**Para:** {msg['to']}\n"
            f"**Asunto:** {msg['subject']}\n"
            f"**Fecha:** {msg['date']}\n"
            f"**Thread:** {msg['thread_id']}\n\n"
            f"---\n{msg['body']}"
        )
    except Exception as e:
        return f"❌ Error: {e}"


@aura_tool(
    name="gmail_search",
    description="Busca emails usando Gmail query syntax. Ej: 'from:cliente@empresa.com subject:presupuesto after:2026/05/01'",
    category="email",
    parameters={
        "query": {"type": "str", "description": "Query Gmail (from:, to:, subject:, after:, before:, is:unread, etc.)"},
        "max_results": {"type": "int", "description": "Máximo resultados (default 10)"},
    },
)
async def gmail_search(query: str, max_results: int = 10) -> str:
    if not gmail_client.is_configured():
        return "⚠️ Gmail no configurado. Envía /gmail-auth para activarlo."
    try:
        msgs = await gmail_client.search(query=query, max_results=max_results)
        if not msgs:
            return f"📭 Sin resultados para: {query}"
        lines = [f"🔍 {len(msgs)} resultado(s) para `{query}`:\n"]
        for m in msgs:
            lines.append(f"📧 ID: `{m['id']}`  |  De: {m['from']}")
            lines.append(f"   Asunto: {m['subject']}  |  {m['date']}")
            lines.append(f"   {m['snippet'][:100]}...")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {e}"


@aura_tool(
    name="gmail_send",
    description=(
        "Envía un email DESDE royaluniondesign@gmail.com a cualquier destinatario. "
        "Usar para respuestas a clientes, presupuestos, propuestas comerciales de RUD Studio."
    ),
    category="email",
    parameters={
        "to": {"type": "str", "description": "Email del destinatario"},
        "subject": {"type": "str", "description": "Asunto del email"},
        "body": {"type": "str", "description": "Cuerpo en texto plano"},
        "html": {"type": "str", "description": "Cuerpo HTML opcional (para emails más elaborados)"},
    },
)
async def gmail_send(to: str, subject: str, body: str, html: str = "") -> str:
    if not gmail_client.is_configured():
        return "⚠️ Gmail no configurado. Envía /gmail-auth para activarlo."
    try:
        result = await gmail_client.send(to=to, subject=subject, body=body, html=html or None)
        if result.get("ok"):
            return f"✅ Email enviado a {to} desde royaluniondesign@gmail.com"
        return f"❌ Error: {result.get('error')}"
    except Exception as e:
        return f"❌ Error: {e}"


@aura_tool(
    name="gmail_reply",
    description="Responde a un email existente manteniendo el hilo. Necesitas el message_id del mensaje original.",
    category="email",
    parameters={
        "message_id": {"type": "str", "description": "ID del mensaje al que respondes"},
        "body": {"type": "str", "description": "Texto de tu respuesta"},
        "html": {"type": "str", "description": "HTML de la respuesta (opcional)"},
    },
)
async def gmail_reply(message_id: str, body: str, html: str = "") -> str:
    if not gmail_client.is_configured():
        return "⚠️ Gmail no configurado. Envía /gmail-auth para activarlo."
    try:
        # Get original to find thread_id and recipient
        original = await gmail_client.get_message(message_id)
        to = original.get("from", "")
        subject = original.get("subject", "")
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"

        result = await gmail_client.send(
            to=to,
            subject=subject,
            body=body,
            html=html or None,
            reply_to_message_id=message_id,
            reply_to_thread_id=original.get("thread_id"),
        )
        if result.get("ok"):
            return f"✅ Respuesta enviada a {to}"
        return f"❌ Error: {result.get('error')}"
    except Exception as e:
        return f"❌ Error: {e}"


@aura_tool(
    name="gmail_status",
    description="Verifica si Gmail está configurado y muestra la cuenta activa.",
    category="email",
    parameters={},
)
async def gmail_status() -> str:
    if gmail_client.is_configured():
        return f"✅ Gmail activo → {gmail_client.RUD_EMAIL}"
    return (
        "⚠️ Gmail no configurado.\n\n"
        "Para activar:\n"
        "1. Envía /gmail-auth\n"
        "2. Sigue las instrucciones para crear credenciales OAuth\n"
        "3. Pega el JSON de las credenciales\n\n"
        "Mientras tanto, el envío funciona solo hacia royaluniondesign@gmail.com (vía Resend)."
    )
