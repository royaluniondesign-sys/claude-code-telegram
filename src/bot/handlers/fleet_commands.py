"""Fleet & SuperNode command handlers — multi-machine orchestration.

All handlers are mixin methods that get composed into MessageOrchestrator.
"""

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..utils.html_format import escape_html

logger = structlog.get_logger()


class FleetCommandsMixin:
    """Mixin: machines, ssh, fleet, nodes, dispatch commands."""

    async def _zt_machines(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Show registered machines."""
        from ...infra.fleet import FleetManager

        fleet = FleetManager()
        await update.message.reply_text(
            fleet.format_fleet_status(), parse_mode="HTML"
        )

    async def _zt_ssh(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Execute command on remote machine: /ssh <machine> <command>."""
        from ...infra.fleet import FleetManager

        fleet = FleetManager()
        args = (update.message.text or "").split(maxsplit=2)

        if len(args) < 3:
            machines = fleet.list_machines()
            if not machines:
                await update.message.reply_text(
                    "Usage: <code>/ssh machine command</code>\n"
                    "No machines registered. Use <code>/fleet add</code> first.",
                    parse_mode="HTML",
                )
                return
            names = ", ".join(f"<code>{m.name}</code>" for m in machines)
            await update.message.reply_text(
                f"Usage: <code>/ssh machine command</code>\n"
                f"Available: {names}",
                parse_mode="HTML",
            )
            return

        machine_name = args[1]
        command = args[2]

        msg = await update.message.reply_text(
            f"⏳ Executing on <b>{escape_html(machine_name)}</b>...",
            parse_mode="HTML",
        )

        result = await fleet.execute(machine_name, command)

        output = escape_html(result.output[:3000])
        status = "✅" if result.success else "❌"
        await msg.edit_text(
            f"{status} <b>{escape_html(machine_name)}</b> ({result.duration_ms}ms)\n"
            f"<code>$ {escape_html(command)}</code>\n\n"
            f"<pre>{output}</pre>",
            parse_mode="HTML",
        )

    async def _zt_fleet(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Fleet management: /fleet add|remove|ping|sync."""
        from ...infra.fleet import FleetManager, MemorySync

        fleet = FleetManager()
        args = (update.message.text or "").split()

        if len(args) < 2:
            await update.message.reply_text(
                "<b>🖥️ Fleet Commands</b>\n\n"
                "<code>/fleet add name user@host [label] [platform]</code>\n"
                "<code>/fleet remove name</code>\n"
                "<code>/fleet ping</code> — check all machines\n"
                "<code>/fleet sync</code> — sync shared memory\n"
                "<code>/fleet sync-init remote_url</code> — init git sync\n"
                "<code>/machines</code> — show fleet status\n"
                "<code>/ssh machine command</code> — run remote command",
                parse_mode="HTML",
            )
            return

        action = args[1].lower()

        if action == "add" and len(args) >= 4:
            name = args[2]
            host = args[3]
            label = args[4] if len(args) > 4 else name
            platform = args[5] if len(args) > 5 else "linux"
            machine = fleet.add_machine(name, host, label, platform)
            await update.message.reply_text(
                f"✅ Added <b>{escape_html(machine.label)}</b> "
                f"(<code>{escape_html(host)}</code>)",
                parse_mode="HTML",
            )

        elif action == "remove" and len(args) >= 3:
            name = args[2]
            if fleet.remove_machine(name):
                await update.message.reply_text(
                    f"✅ Removed <b>{escape_html(name)}</b>", parse_mode="HTML"
                )
            else:
                await update.message.reply_text(
                    f"❌ Machine '{escape_html(name)}' not found.", parse_mode="HTML"
                )

        elif action == "ping":
            msg = await update.message.reply_text("⏳ Pinging all machines...")
            results = await fleet.ping_all()
            if not results:
                await msg.edit_text("No machines registered.")
                return
            lines = ["<b>🏓 Ping Results</b>\n"]
            for name, ok in results.items():
                icon = "🟢" if ok else "🔴"
                lines.append(f"{icon} {escape_html(name)}")
            await msg.edit_text("\n".join(lines), parse_mode="HTML")

        elif action == "sync":
            mem = MemorySync()
            if not mem.is_git_repo:
                await update.message.reply_text(
                    "Memory not a git repo. Use <code>/fleet sync-init &lt;remote&gt;</code>",
                    parse_mode="HTML",
                )
                return
            msg = await update.message.reply_text("⏳ Syncing memory...")
            result = await mem.sync()
            if result.get("success", False):
                await msg.edit_text("✅ Memory synced.")
            else:
                await msg.edit_text(f"❌ Sync failed: {result.get('error', '?')}")

        elif action == "sync-init" and len(args) >= 3:
            remote = args[2]
            mem = MemorySync()
            msg = await update.message.reply_text("⏳ Initializing memory git repo...")
            ok = await mem.init_repo(remote)
            if ok:
                await msg.edit_text(
                    f"✅ Memory repo initialized.\nRemote: <code>{escape_html(remote)}</code>",
                    parse_mode="HTML",
                )
            else:
                await msg.edit_text("❌ Failed to initialize repo.")

        else:
            await update.message.reply_text(
                "Unknown fleet command. Try <code>/fleet</code> for help.",
                parse_mode="HTML",
            )

    # --- SuperNode commands ---

    async def _zt_nodes(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Show SuperNode status and profiles."""
        from ...infra.fleet import FleetManager
        from ...infra.supernodes import SuperNodeManager

        fleet = FleetManager()
        nodes = SuperNodeManager(fleet)
        args = (update.message.text or "").split()

        if len(args) >= 3 and args[1] == "profile":
            machine_name = args[2]
            msg = await update.message.reply_text(
                f"⏳ Auto-profiling <b>{escape_html(machine_name)}</b>...",
                parse_mode="HTML",
            )
            profile = await nodes.auto_profile(machine_name)
            if profile:
                await msg.edit_text(
                    f"✅ <b>{escape_html(machine_name)}</b> profiled\n\n"
                    f"RAM: {profile.ram_gb}GB · CPU: {profile.cpu_cores} cores\n"
                    f"GPU: {'✅ ' + str(profile.gpu_vram_gb) + 'GB' if profile.has_gpu else '❌'}\n"
                    f"Tools: {', '.join(profile.tools[:10]) or 'none'}\n"
                    f"Specializations: {', '.join(profile.specializations) or 'general'}\n"
                    f"Score: {int(profile.capability_score)}",
                    parse_mode="HTML",
                )
            else:
                await msg.edit_text(
                    f"❌ Could not profile {escape_html(machine_name)}. Is it reachable?",
                    parse_mode="HTML",
                )
            return

        await update.message.reply_text(
            nodes.format_nodes_status(), parse_mode="HTML"
        )

    async def _zt_dispatch(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Dispatch task to best SuperNode: /dispatch [type] command."""
        from ...infra.fleet import FleetManager
        from ...infra.supernodes import SuperNodeManager, TaskType

        fleet = FleetManager()
        nodes = SuperNodeManager(fleet)
        args = (update.message.text or "").split(maxsplit=2)

        if len(args) < 2:
            await update.message.reply_text(
                "<b>🚀 Dispatch — Auto-Route Tasks</b>\n\n"
                "<code>/dispatch command</code> — auto-pick best node\n"
                "<code>/dispatch render ffmpeg ...</code> — route to render node\n"
                "<code>/dispatch code pytest ...</code> — route to code node\n"
                "<code>/dispatch build make -j8</code> — route to build node\n"
                "<code>/dispatch ai ollama run ...</code> — route to AI node\n"
                "<code>/dispatch data python process.py</code> — route to data node\n\n"
                "Types: render, code, build, ai, data, general\n"
                "Profile nodes first: <code>/nodes profile machine-name</code>",
                parse_mode="HTML",
            )
            return

        valid_types = {t.value for t in TaskType}
        if len(args) >= 3 and args[1] in valid_types:
            task_type = TaskType(args[1])
            command = args[2]
        else:
            task_type = TaskType.GENERAL
            command = (update.message.text or "").split(maxsplit=1)[1]

        best = nodes.best_node_for(task_type)
        if not best:
            await update.message.reply_text(
                "❌ No available node for this task.\n"
                "Register and profile nodes first:\n"
                "<code>/fleet add name user@host</code>\n"
                "<code>/nodes profile name</code>",
                parse_mode="HTML",
            )
            return

        profile = nodes.get_profile(best)
        score_info = f"score {int(profile.score_for_task(task_type))}" if profile else ""

        msg = await update.message.reply_text(
            f"🚀 Dispatching to <b>{escape_html(best)}</b> ({score_info})\n"
            f"Type: {task_type.value}\n"
            f"<code>$ {escape_html(command[:100])}</code>",
            parse_mode="HTML",
        )

        result = await nodes.dispatch(command, task_type=task_type)

        output = escape_html(
            (result.ssh_result.output if result.ssh_result else result.error or "?")[:3000]
        )
        status = "✅" if result.success else "❌"
        await msg.edit_text(
            f"{status} <b>{escape_html(result.node_name)}</b> · "
            f"{result.task_type} · {result.duration_ms}ms\n"
            f"<code>$ {escape_html(command[:80])}</code>\n\n"
            f"<pre>{output}</pre>",
            parse_mode="HTML",
        )
