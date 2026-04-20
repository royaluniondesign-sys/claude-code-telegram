"""Main entry point for Claude Code Telegram Bot."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from src import __version__
from src.bot.core import ClaudeCodeBot
from src.claude import (
    ClaudeIntegration,
    SessionManager,
)
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.features import FeatureFlags
from src.config.settings import Settings
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.middleware import EventSecurityMiddleware
from src.exceptions import ConfigurationError
from src.notifications.service import NotificationService
from src.projects import ProjectThreadManager, load_project_registry
from src.scheduler.scheduler import JobScheduler
from src.security.audit import AuditLogger, InMemoryAuditStorage
from src.security.auth import (
    AuthenticationManager,
    InMemoryTokenStorage,
    TokenAuthProvider,
    WhitelistAuthProvider,
)
from src.security.rate_limiter import RateLimiter
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.session_storage import SQLiteSessionStorage


import time

_LAST_MEMORY_WARN: float = 0.0


def setup_logging(debug: bool = False) -> None:
    """Configure structured logging."""
    level = logging.DEBUG if debug else logging.INFO

    # Configure standard logging
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            (
                structlog.processors.JSONRenderer()
                if not debug
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Claude Code Telegram Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version", action="version", version=f"Claude Code Telegram Bot {__version__}"
    )

    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    parser.add_argument("--config-file", type=Path, help="Path to configuration file")

    return parser.parse_args()


async def create_application(config: Settings) -> Dict[str, Any]:
    """Create and configure the application components."""
    logger = structlog.get_logger()
    logger.info("Creating application components")

    features = FeatureFlags(config)

    # Initialize storage system
    storage = Storage(config.database_url)
    await storage.initialize()

    # Create security components
    providers = []

    # Add whitelist provider if users are configured
    if config.allowed_users:
        providers.append(WhitelistAuthProvider(config.allowed_users))

    # Add token provider if enabled
    if config.enable_token_auth:
        token_storage = InMemoryTokenStorage()  # TODO: Use database storage
        providers.append(TokenAuthProvider(config.auth_token_secret, token_storage))

    # Fall back to allowing all users in development mode
    if not providers and config.development_mode:
        logger.warning(
            "No auth providers configured"
            " - creating development-only allow-all provider"
        )
        providers.append(WhitelistAuthProvider([], allow_all_dev=True))
    elif not providers:
        raise ConfigurationError("No authentication providers configured")

    auth_manager = AuthenticationManager(providers)
    security_validator = SecurityValidator(
        config.approved_directory,
        disable_security_patterns=config.disable_security_patterns,
    )
    rate_limiter = RateLimiter(config)

    # Create audit storage and logger
    audit_storage = InMemoryAuditStorage()  # TODO: Use database storage in production
    audit_logger = AuditLogger(audit_storage)

    # Create Claude integration components with persistent storage
    session_storage = SQLiteSessionStorage(storage.db_manager)
    session_manager = SessionManager(config, session_storage)

    # Create Claude SDK manager and integration facade
    logger.info("Using Claude Python SDK integration")
    sdk_manager = ClaudeSDKManager(config, security_validator=security_validator)

    claude_integration = ClaudeIntegration(
        config=config,
        sdk_manager=sdk_manager,
        session_manager=session_manager,
    )

    # --- Event bus and agentic platform components ---
    event_bus = EventBus()

    # Event security middleware
    event_security = EventSecurityMiddleware(
        event_bus=event_bus,
        security_validator=security_validator,
        auth_manager=auth_manager,
    )
    event_security.register()

    # Agent handler — translates events into Claude executions
    agent_handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=claude_integration,
        default_working_directory=config.approved_directory,
        default_user_id=config.allowed_users[0] if config.allowed_users else 0,
    )
    agent_handler.register()

    # Create multi-brain router
    from src.brains.router import BrainRouter

    brain_router = BrainRouter()
    # Claude excluded from router — only Gemini + Codex (saves MAX plan quota)
    logger.info("Brain router initialized", brains=brain_router.available_brains)

    # Initialize conductor singleton (3-layer orchestrator)
    from src.brains.conductor import Conductor, set_conductor
    set_conductor(Conductor(brain_router))
    logger.info("Conductor initialized (3-layer orchestrator ready)")

    # Rate limit monitor (persists usage to ~/.aura/usage.json)
    # Use the global singleton so conductor track_request() calls and /limits share one instance
    from src.infra.rate_monitor import get_global_monitor
    import src.infra.rate_monitor as _rm_module

    rate_monitor = get_global_monitor()
    _rm_module._global_monitor = rate_monitor  # ensure singleton is the one we just created

    # Create bot with all dependencies
    dependencies = {
        "auth_manager": auth_manager,
        "security_validator": security_validator,
        "rate_limiter": rate_limiter,
        "audit_logger": audit_logger,
        "claude_integration": claude_integration,
        "brain_router": brain_router,
        "rate_monitor": rate_monitor,
        "storage": storage,
        "event_bus": event_bus,
        "project_registry": None,
        "project_threads_manager": None,
    }

    bot = ClaudeCodeBot(config, dependencies)

    # Notification service and scheduler need the bot's Telegram Bot instance,
    # which is only available after bot.initialize(). We store placeholders
    # and wire them up in run_application() after initialization.

    logger.info("Application components created successfully")

    return {
        "bot": bot,
        "claude_integration": claude_integration,
        "storage": storage,
        "config": config,
        "features": features,
        "event_bus": event_bus,
        "agent_handler": agent_handler,
        "auth_manager": auth_manager,
        "security_validator": security_validator,
        "brain_router": brain_router,
        "rate_monitor": rate_monitor,
    }


async def run_application(app: Dict[str, Any]) -> None:
    """Run the application with graceful shutdown handling."""
    logger = structlog.get_logger()
    bot: ClaudeCodeBot = app["bot"]
    claude_integration: ClaudeIntegration = app["claude_integration"]
    storage: Storage = app["storage"]
    config: Settings = app["config"]
    features: FeatureFlags = app["features"]
    event_bus: EventBus = app["event_bus"]
    brain_router = app.get("brain_router")
    rate_monitor = app.get("rate_monitor")

    notification_service: Optional[NotificationService] = None
    scheduler: Optional[JobScheduler] = None
    project_threads_manager: Optional[ProjectThreadManager] = None

    # Set up signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(signum: int, frame: Any) -> None:
        logger.info("Shutdown signal received", signal=signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("Starting Claude Code Telegram Bot")

        # Initialize the bot first (creates the Telegram Application)
        await bot.initialize()

        # Load persisted voice-on user IDs so /voz state survives restarts
        try:
            from src.bot.features.voice_tts import load_voice_prefs
            _voice_users = load_voice_prefs()
            bot.deps["voice_users"] = _voice_users
            if _voice_users:
                logger.info("voice_prefs_loaded", users=len(_voice_users))
        except Exception as _ve:
            logger.warning("voice_prefs_load_failed", error=str(_ve))
            bot.deps.setdefault("voice_users", set())

        if config.enable_project_threads:
            if not config.projects_config_path:
                raise ConfigurationError(
                    "Project thread mode enabled but required settings are missing"
                )
            registry = load_project_registry(
                config_path=config.projects_config_path,
                approved_directory=config.approved_directory,
            )
            project_threads_manager = ProjectThreadManager(
                registry=registry,
                repository=storage.project_threads,
                sync_action_interval_seconds=(
                    config.project_threads_sync_action_interval_seconds
                ),
            )

            bot.deps["project_registry"] = registry
            bot.deps["project_threads_manager"] = project_threads_manager

            if config.project_threads_mode == "group":
                if config.project_threads_chat_id is None:
                    raise ConfigurationError(
                        "Group thread mode requires PROJECT_THREADS_CHAT_ID"
                    )
                sync_result = await project_threads_manager.sync_topics(
                    bot.app.bot,
                    chat_id=config.project_threads_chat_id,
                )
                logger.info(
                    "Project thread startup sync complete",
                    mode=config.project_threads_mode,
                    chat_id=config.project_threads_chat_id,
                    created=sync_result.created,
                    reused=sync_result.reused,
                    renamed=sync_result.renamed,
                    failed=sync_result.failed,
                    deactivated=sync_result.deactivated,
                )

        # Now wire up components that need the Telegram Bot instance
        telegram_bot = bot.app.bot

        # Start event bus
        await event_bus.start()

        # Notification service
        notification_service = NotificationService(
            event_bus=event_bus,
            bot=telegram_bot,
            default_chat_ids=config.notification_chat_ids or [],
        )
        notification_service.register()
        await notification_service.start()

        # Collect concurrent tasks
        tasks = []

        # Bot task — use start() which handles its own initialization check
        bot_task = asyncio.create_task(bot.start())
        tasks.append(bot_task)

        # API server (if enabled)
        if features.api_server_enabled:
            from src.api.server import run_api_server

            api_task = asyncio.create_task(
                run_api_server(
                    event_bus, config, storage.db_manager,
                    brain_router=brain_router,
                    rate_monitor=rate_monitor,
                )
            )
            tasks.append(api_task)
            logger.info("API server enabled", port=config.api_server_port)

        # Scheduler (if enabled)
        if features.scheduler_enabled:
            scheduler = JobScheduler(
                event_bus=event_bus,
                db_manager=storage.db_manager,
                default_working_directory=config.approved_directory,
            )
            await scheduler.start()
            logger.info("Job scheduler enabled")

            # Register business workflows (Phase 6)
            from src.workflows.scheduler_setup import register_workflows

            owner_chat_id = (config.notification_chat_ids or [0])[0]
            if owner_chat_id:
                wf_names = register_workflows(scheduler, telegram_bot, owner_chat_id)
                logger.info("Business workflows registered", workflows=wf_names)

            # Init routine runner — loads all user-defined routines from DB
            # NOTE: notify_fn is set later (after _notify_proactive is defined below)
            try:
                from src.scheduler.routine_runner import (
                    init_routine_runner, load_all_routines,
                )
                init_routine_runner(
                    scheduler._scheduler,  # APScheduler instance
                    brain_router,
                    notify_fn=None,  # Updated after _notify_proactive is defined
                )
                n = await load_all_routines()
                logger.info("Routines loaded", count=n)
            except Exception as _re:
                logger.warning("routines_init_failed", error=str(_re))

        # RAG — local vector memory (fire-and-forget: do NOT add to tasks list)
        try:
            from src.rag.indexer import RAGIndexer
            _rag_indexer = RAGIndexer()
            # One-shot background index — intentionally not in tasks list.
            # If added to tasks + FIRST_COMPLETED, it triggers full shutdown when done.
            asyncio.create_task(_rag_indexer.index_all_background(), name="rag_init")
            logger.info("rag_indexer_started")
        except Exception as _rag_err:
            logger.warning("rag_init_failed", error=str(_rag_err))

        # Watchdog — check services every 5 minutes, self-heal
        # notify=None: watchdog is SILENT — no Telegram spam. Logs warnings to file only.
        async def _watchdog_loop() -> None:
            from src.infra.watchdog import Watchdog

            dog = Watchdog(notify_callback=None)
            while not shutdown_event.is_set():
                try:
                    await asyncio.sleep(300)  # 5 minutes
                    report = await dog.check_and_heal()
                    if not report.all_healthy:
                        global _LAST_MEMORY_WARN
                        # Suppress repeated high-memory-only warnings — rate limit to once per 15 min
                        mem_only = report.memory_used_pct > 0 and all(
                            "High memory" in w for w in report.warnings
                        ) and all(s.is_running for s in report.services)
                        if mem_only:
                            now = time.time()
                            if report.memory_used_pct <= 99 or now - _LAST_MEMORY_WARN < 900:
                                pass  # skip — below new threshold or too soon
                            else:
                                _LAST_MEMORY_WARN = now
                                logger.warning("watchdog_issues", warnings=report.warnings)
                        else:
                            logger.warning("watchdog_issues", warnings=report.warnings)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("watchdog_error", error=str(e))

        watchdog_task = asyncio.create_task(_watchdog_loop())
        tasks.append(watchdog_task)
        logger.info("Watchdog started (5min interval)")

        # Auto-executor — picks up auto_fix tasks every 5 min, self-evaluates every 30 min
        # notify=None: auto_executor is SILENT — no Telegram spam. Logs to file only.
        # The proactive conductor handles all meaningful Telegram notifications.
        from src.infra.auto_executor import auto_executor_loop

        auto_exec_task = asyncio.create_task(auto_executor_loop(notify=None))
        tasks.append(auto_exec_task)
        logger.info("Auto-executor started (5min exec, 30min eval interval)")

        # Brain recovery monitor — watch rate-limited brains, notify when they come back
        async def _brain_recovery_monitor() -> None:
            """Poll rate-limited brains every 60s, notify + auto-switch when recovered."""
            from src.infra.rate_monitor import get_global_monitor
            _was_rate_limited: dict = {}
            await asyncio.sleep(30)  # startup delay
            while True:
                try:
                    monitor = get_global_monitor()
                    for usage in monitor.get_all_usage():
                        name = usage.brain_name
                        was_rl = _was_rate_limited.get(name, False)
                        is_rl = usage.is_rate_limited

                        if is_rl and not was_rl:
                            # Just became rate limited
                            _was_rate_limited[name] = True
                            logger.info("brain_rate_limited", brain=name,
                                        recover_in=usage.recover_in_str)
                            msg = (
                                f"⛔ *{name}* rate limited\n"
                                f"Recupera en: `{usage.recover_in_str}`\n"
                                f"Usado: {usage.requests_in_window}/{usage.known_limit}"
                            )
                            for cid in (config.notification_chat_ids or []):
                                try:
                                    await telegram_bot.send_message(cid, msg, parse_mode="Markdown")
                                except Exception:
                                    pass

                        elif was_rl and not is_rl:
                            # Rate limit cleared — brain available again
                            _was_rate_limited[name] = False
                            logger.info("brain_recovered", brain=name)
                            msg = f"✅ *{name}* disponible de nuevo — tokens recuperados"
                            for cid in (config.notification_chat_ids or []):
                                try:
                                    await telegram_bot.send_message(cid, msg, parse_mode="Markdown")
                                except Exception:
                                    pass
                except Exception as e:
                    logger.warning("brain_recovery_monitor_error", error=str(e))
                await asyncio.sleep(60)

        brain_recovery_task = asyncio.create_task(_brain_recovery_monitor())
        tasks.append(brain_recovery_task)

        # Semantic Router + MemPalace load lazily on first use (saves ~300MB RAM at startup)
        logger.info("AI stack: lazy load enabled (semantic router + MemPalace on first use)")

        # Auto-register AURA MCP with all available CLIs (background, non-blocking)
        async def _register_mcp_clients() -> None:
            try:
                import asyncio as _asyncio
                loop = _asyncio.get_event_loop()
                from src.mcp.cli_registrar import register_all
                results = await loop.run_in_executor(None, register_all)
                logger.info("mcp_registered", clients=results)
            except Exception as e:
                logger.warning("mcp_registration_error", error=str(e))

        # One-shot MCP registration — intentionally not in tasks list (same reason as rag_init).
        asyncio.create_task(_register_mcp_clients(), name="mcp_reg")

        # Proactive conductor loop — autonomous AURA self-improvement every 15 min
        _notify_timestamps: list = []  # rolling window for rate limiting
        _NOTIFY_MAX_PER_HOUR = 4       # max proactive notifications per hour (reduced to avoid flood)

        async def _notify_proactive(msg: str) -> None:
            nonlocal _notify_timestamps
            import time as _time
            now = _time.time()
            # Respect global Telegram flood ban (shared with orchestrator)
            try:
                from src.bot.flood_guard import remaining_flood_wait, set_flood_wait, extract_retry_after
                flood_remaining = remaining_flood_wait()
                if flood_remaining > 0:
                    logger.info("proactive_notify_skipped_flood", remaining_s=flood_remaining)
                    return
            except Exception:
                pass
            # Per-hour rate limit: drop oldest outside 1h window
            _notify_timestamps = [t for t in _notify_timestamps if now - t < 3600]
            if len(_notify_timestamps) >= _NOTIFY_MAX_PER_HOUR:
                logger.info("proactive_notify_skipped_hourly_cap",
                            sent=len(_notify_timestamps), cap=_NOTIFY_MAX_PER_HOUR)
                return
            for cid in (config.notification_chat_ids or []):
                try:
                    await telegram_bot.send_message(cid, msg, parse_mode="HTML")
                    _notify_timestamps.append(now)
                except Exception as e:
                    err = str(e)
                    if "429" in err or "Too Many Requests" in err:
                        try:
                            from src.bot.flood_guard import set_flood_wait, extract_retry_after
                            wait = extract_retry_after(err) or 3600
                            set_flood_wait(wait)
                        except Exception:
                            pass
                        logger.warning("proactive_notify_flood_wait", error=err[:80])
                    else:
                        logger.warning("proactive_notify_fail", error=err[:100])

        from src.infra.proactive_loop import start_proactive_loop
        proactive_task = asyncio.create_task(
            start_proactive_loop(brain_router, notify_fn=_notify_proactive)
        )
        tasks.append(proactive_task)
        logger.info("Proactive conductor loop started (15min autonomous self-improvement)")

        # Wire notify_fn into routine_runner now that _notify_proactive is defined
        try:
            from src.scheduler.routine_runner import set_notify_fn
            set_notify_fn(_notify_proactive)
            logger.info("routine_runner_notify_fn_wired")
        except Exception:
            pass

        # Watchdog — active Telegram ping every 2 min, auto-restart after 3 failures
        _bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if _bot_token:
            from src.infra.watchdog import run_ping_loop
            watchdog_task = asyncio.create_task(run_ping_loop(_bot_token))
            tasks.append(watchdog_task)
            logger.info("Watchdog ping loop started (2min interval, 3-strike restart)")

        # AURA Dashboard (always-on, port 3000)
        async def _dashboard_loop() -> None:
            from src.dashboard.app import run_dashboard, set_deps

            # Share live instances with the dashboard
            brain_router = bot.deps.get("brain_router")
            rate_monitor_dep = bot.deps.get("rate_monitor")
            set_deps(brain_router, rate_monitor_dep)

            try:
                dash_port = int(os.environ.get("DASHBOARD_PORT", "3000"))
                await run_dashboard(host="0.0.0.0", port=dash_port)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("dashboard_error", error=str(e))

        dashboard_task = asyncio.create_task(_dashboard_loop())
        tasks.append(dashboard_task)
        dash_port = os.environ.get("DASHBOARD_PORT", "3000")
        logger.info("AURA Dashboard started", url=f"http://localhost:{dash_port}")

        # Shutdown task
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        tasks.append(shutdown_task)

        # Wait for any task to complete or shutdown signal
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Check completed tasks for exceptions
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Task failed",
                    task=task.get_name(),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error("Application error", error=str(e))
        raise
    finally:
        # Ordered shutdown: scheduler -> API -> notification -> bot -> claude -> storage
        logger.info("Shutting down application")

        try:
            if scheduler:
                await scheduler.stop()
            if notification_service:
                await notification_service.stop()
            await event_bus.stop()
            await bot.stop()
            await claude_integration.shutdown()
            await storage.close()
        except Exception as e:
            logger.error("Error during shutdown", error=str(e))

        logger.info("Application shutdown complete")


async def main() -> None:
    """Main application entry point."""
    args = parse_args()
    setup_logging(debug=args.debug)

    logger = structlog.get_logger()
    logger.info("Starting Claude Code Telegram Bot", version=__version__)

    try:
        # Load configuration
        from src.config import FeatureFlags, load_config

        config = load_config(config_file=args.config_file)
        features = FeatureFlags(config)

        logger.info(
            "Configuration loaded",
            environment="production" if config.is_production else "development",
            enabled_features=features.get_enabled_features(),
            debug=config.debug,
        )

        # Initialize bot and Claude integration
        app = await create_application(config)
        await run_application(app)

    except ConfigurationError as e:
        logger.error("Configuration error", error=str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error", error=str(e))
        sys.exit(1)


def run() -> None:
    """Synchronous entry point for setuptools."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested by user")
        sys.exit(0)


if __name__ == "__main__":
    run()
