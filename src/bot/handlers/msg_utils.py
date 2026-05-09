"""Message utility functions: cost estimation, placeholder responses, working directory updates."""

import re
from pathlib import Path
from typing import Optional

import structlog
from telegram.ext import ContextTypes

from ...config.settings import Settings

logger = structlog.get_logger()


def _estimate_text_processing_cost(text: str) -> float:
    """Estimate cost for processing text message."""
    # Base cost
    base_cost = 0.001

    # Additional cost based on length
    length_cost = len(text) * 0.00001

    # Additional cost for complex requests
    complex_keywords = [
        "analyze",
        "generate",
        "create",
        "build",
        "implement",
        "refactor",
        "optimize",
        "debug",
        "explain",
        "document",
    ]

    text_lower = text.lower()
    complexity_multiplier = 1.0

    for keyword in complex_keywords:
        if keyword in text_lower:
            complexity_multiplier += 0.5

    return (base_cost + length_cost) * min(complexity_multiplier, 3.0)


def _estimate_file_processing_cost(file_size: int) -> float:
    """Estimate cost for processing uploaded file."""
    # Base cost for file handling
    base_cost = 0.005

    # Additional cost based on file size (per KB)
    size_cost = (file_size / 1024) * 0.0001

    return base_cost + size_cost


async def _generate_placeholder_response(
    message_text: str, context: ContextTypes.DEFAULT_TYPE
) -> dict:
    """Generate placeholder response until Claude integration is implemented."""
    settings: Settings = context.bot_data["settings"]
    current_dir = getattr(
        context.user_data, "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Analyze the message for intent
    message_lower = message_text.lower()

    if any(
        word in message_lower for word in ["list", "show", "see", "directory", "files"]
    ):
        response_text = (
            f"🤖 <b>Claude Code Response</b> <i>(Placeholder)</i>\n\n"
            f"I understand you want to see files. Try using the /ls command to list files "
            f"in your current directory (<code>{relative_path}/</code>).\n\n"
            f"<b>Available commands:</b>\n"
            f"• /ls - List files\n"
            f"• /cd &lt;dir&gt; - Change directory\n"
            f"• /projects - Show projects\n\n"
            f"<i>Note: Full Claude Code integration will be available in the next phase.</i>"
        )

    elif any(word in message_lower for word in ["create", "generate", "make", "build"]):
        response_text = (
            f"🤖 <b>Claude Code Response</b> <i>(Placeholder)</i>\n\n"
            f"I understand you want to create something! Once the Claude Code integration "
            f"is complete, I'll be able to:\n\n"
            f"• Generate code files\n"
            f"• Create project structures\n"
            f"• Write documentation\n"
            f"• Build complete applications\n\n"
            f"<b>Current directory:</b> <code>{relative_path}/</code>\n\n"
            f"<i>Full functionality coming soon!</i>"
        )

    elif any(word in message_lower for word in ["help", "how", "what", "explain"]):
        response_text = (
            "🤖 <b>Claude Code Response</b> <i>(Placeholder)</i>\n\n"
            "I'm here to help! Try using /help for available commands.\n\n"
            "<b>What I can do now:</b>\n"
            "• Navigate directories (/cd, /ls, /pwd)\n"
            "• Show projects (/projects)\n"
            "• Manage sessions (/new, /status)\n\n"
            "<b>Coming soon:</b>\n"
            "• Full Claude Code integration\n"
            "• Code generation and editing\n"
            "• File operations\n"
            "• Advanced programming assistance"
        )

    else:
        response_text = (
            f"🤖 <b>Claude Code Response</b> <i>(Placeholder)</i>\n\n"
            f"I received your message: \"{message_text[:100]}{'...' if len(message_text) > 100 else ''}\"\n\n"
            f"<b>Current Status:</b>\n"
            f"• Directory: <code>{relative_path}/</code>\n"
            f"• Bot core: ✅ Active\n"
            f"• Claude integration: 🔄 Coming soon\n\n"
            f"Once Claude Code integration is complete, I'll be able to process your "
            f"requests fully and help with coding tasks!\n\n"
            f"For now, try the available commands like /ls, /cd, and /help."
        )

    return {"text": response_text, "parse_mode": "HTML"}


def _update_working_directory_from_claude_response(
    claude_response, context, settings, user_id
):
    """Update the working directory based on Claude's response content."""
    # Look for directory changes in Claude's response
    # This searches for common patterns that indicate directory changes
    patterns = [
        r"(?:^|\n).*?cd\s+([^\s\n]+)",  # cd command
        r"(?:^|\n).*?Changed directory to:?\s*([^\s\n]+)",  # explicit directory change
        r"(?:^|\n).*?Current directory:?\s*([^\s\n]+)",  # current directory indication
        r"(?:^|\n).*?Working directory:?\s*([^\s\n]+)",  # working directory indication
    ]

    content = claude_response.content.lower()
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    for pattern in patterns:
        matches = re.findall(pattern, content, re.MULTILINE | re.IGNORECASE)
        for match in matches:
            try:
                # Clean up the path
                new_path = match.strip().strip("\"'`")

                # Handle relative paths
                if new_path.startswith("./") or new_path.startswith("../"):
                    new_path = (current_dir / new_path).resolve()
                elif not new_path.startswith("/"):
                    # Relative path without ./
                    new_path = (current_dir / new_path).resolve()
                else:
                    # Absolute path
                    new_path = Path(new_path).resolve()

                # Validate that the new path is within the approved directory
                if (
                    new_path.is_relative_to(settings.approved_directory)
                    and new_path.exists()
                ):
                    context.user_data["current_directory"] = new_path
                    logger.info(
                        "Updated working directory from Claude response",
                        old_dir=str(current_dir),
                        new_dir=str(new_path),
                        user_id=user_id,
                    )
                    return  # Take the first valid match

            except (ValueError, OSError) as e:
                # Invalid path, skip this match
                logger.debug(
                    "Invalid path in Claude response", path=match, error=str(e)
                )
                continue
