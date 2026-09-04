"""Inline-button callback handlers for the Telegram bot interface."""

from __future__ import annotations

import functools
import html
import logging
import os
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from telegram_commands import _apply_mode, _MODE_DESCRIPTIONS

if TYPE_CHECKING:
    from telegram_interface import TelegramInterface

    assert TelegramInterface

logger = logging.getLogger(__name__)


async def _ack_query(query) -> None:
    """Best-effort button-press acknowledgment (Telegram requires within ~10 s)."""
    try:
        await query.answer()
    except Exception as exc:
        logger.warning("query.answer() failed: %s", exc)


def _require_cb_auth(fn):
    """Decorator: reject unauthorized callers before running a callback handler."""

    @functools.wraps(fn)
    async def _wrapper(
        iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        caller = query.from_user
        caller_id = caller.id if caller else None
        if caller_id is None or not iface._is_authorized(caller_id):
            try:
                await query.answer("⛔ Not authorized.", show_alert=True)
            except Exception:
                pass
            return
        return await fn(iface, update, ctx)

    return _wrapper


@_require_cb_auth
async def cb_confirm(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Confirm / Till /reset / Deny confirmation button presses.

    For grant-capable file tools, the pressed button creates a corresponding
    grant on the depth-0 ledger (prompt or session lifetime) before resuming
    the agent. Shell/secret_get never receive standing grants — their prompts
    only render Confirm/Deny and no ledger entry is created.
    """
    query = update.callback_query
    data = query.data  # "confirm_yes:<token>" | "confirm_no:<token>" | "confirm_till_reset:<token>"

    action = data.split(":", 1)[0]
    token = data.split(":", 1)[1]
    confirmed = action != "confirm_no"
    is_session = action == "confirm_till_reset"

    builtin = getattr(getattr(iface, "agent", None), "builtin_executor", None)
    tool_name = ""
    zone_path = ""
    if builtin is not None:
        entry = builtin._pending.get(token)
        if entry is not None:
            tool_name, _args = entry
        zone_path = builtin._zone_paths.get(token, "")

    logger.info(
        "Confirmation callback: action=%s token=%s agent=%s",
        action, token[:8], "set" if iface.agent else "None",
    )

    # Grant bookkeeping before resume, but never let grant failure lose the op.
    if confirmed and iface.agent and tool_name and zone_path:
        try:
            from confirmation import GrantLifetime

            ledger = iface.agent._confirmation.grant_ledger
            grant_dir = os.path.dirname(os.path.realpath(zone_path))
            lifetime = GrantLifetime.SESSION if is_session else GrantLifetime.PROMPT
            ledger.add(tool_name, grant_dir, lifetime, scope_owner=None)
        except Exception as exc:
            logger.warning(
                "cb_confirm: grant bookkeeping failed for token=%s: %s",
                token[:8], exc, exc_info=True,
            )

    # Resume the agent FIRST — before any Telegram API calls that might fail
    if iface.agent:
        iface.agent.resume(token, confirmed)
    else:
        logger.warning("_cb_confirm: iface.agent is None — cannot resume agent")

    # Acknowledge the button press (best-effort; Telegram requires this within ~10s)
    await _ack_query(query)

    # Edit the message to reflect the decision (best-effort)
    if confirmed:
        if is_session:
            result_text = "✅✅ Confirmed until /reset — executing…"
        else:
            result_text = "✅ Confirmed for this prompt — executing…"
    else:
        result_text = "❌ Denied."
    try:
        await query.edit_message_text(
            f"⚠️ <b>Confirmation</b>\n\n{result_text}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.debug("Could not edit confirmation message: %s", exc)


@_require_cb_auth
async def cb_extend(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Extend / Unlimited / Cancel button presses for max-steps extension."""
    query = update.callback_query
    data = query.data  # "extend_yes:<token>" | "extend_unlimited:<token>" | "extend_no:<token>"

    if data.startswith("extend_unlimited:"):
        token = data.split(":", 1)[1]
        response = "unlimited"
        result_text = "♾️ Running until done…"
    elif data.startswith("extend_yes:"):
        token = data.split(":", 1)[1]
        response = "yes"
        result_text = "⏩ Extending…"
    else:
        token = data.split(":", 1)[1]
        response = "no"
        result_text = "❌ Cancelled."

    if iface.agent:
        iface.agent.resume_extend(token, response)
    else:
        logger.warning("_cb_extend: agent is None")

    await _ack_query(query)

    try:
        await query.edit_message_text(
            f"⏱ <b>Max steps</b>\n\n{result_text}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.debug("Could not edit extend message: %s", exc)


@_require_cb_auth
async def cb_llm_retry(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Retry / Cancel button presses for LLM error recovery."""
    query = update.callback_query
    data = query.data  # "llm_retry:{token}:retry" | "llm_retry:{token}:cancel"

    # Parse: llm_retry:{token}:{response}
    parts = data.split(":", 2)
    token = parts[1] if len(parts) > 1 else ""
    response = parts[2] if len(parts) > 2 else "cancel"

    if response == "retry":
        result_text = "🔄 Retrying…"
    else:
        result_text = "❌ Cancelled."

    # Signal the agent thread via the public resume method (same pattern as
    # cb_confirm → iface.agent.resume and cb_extend → iface.agent.resume_extend).
    if iface.agent and hasattr(iface.agent, 'resume_llm_error'):
        iface.agent.resume_llm_error(token, response)
    else:
        logger.warning("cb_llm_retry: agent or resume_llm_error method not available")

    await _ack_query(query)

    try:
        await query.edit_message_text(
            f"⚠️ <b>LLM Error</b>\n\n{result_text}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.debug("Could not edit LLM error message: %s", exc)


@_require_cb_auth
async def cb_model_switch(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle model switch button presses."""
    query = update.callback_query
    await _ack_query(query)
    model_name = query.data.split(":", 1)[1]

    if iface.llm_client and hasattr(iface.llm_client, "set_model"):
        success = iface.llm_client.set_model(model_name)
        if success:
            active = iface.llm_client.llm_cfg
            text = (
                f"✅ Switched to <b>{html.escape(active.get('name', model_name))}</b>"
                f" (<code>{html.escape(model_name)}</code>)\n"
                f"<i>Takes effect from your next message.</i>"
            )
        else:
            text = f"❌ Model <code>{html.escape(model_name)}</code> not found."
    else:
        text = "❌ Model switching not available."

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


@_require_cb_auth
async def cb_mode_switch(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle creativity-mode switch button presses."""
    query = update.callback_query
    await _ack_query(query)
    new_mode = query.data.split(":", 1)[1]

    if new_mode not in _MODE_DESCRIPTIONS:
        text = f"❌ Unknown mode <code>{html.escape(new_mode)}</code>."
    else:
        _apply_mode(iface, new_mode)
        text = (
            f"🎭 <b>Mode: {html.escape(new_mode)}</b>\n"
            f"<i>{html.escape(_MODE_DESCRIPTIONS[new_mode])}</i>\n"
            f"<i>Takes effect from your next task.</i>"
        )

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


@_require_cb_auth
async def cb_deferred(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Run / Discard buttons for a deferred agent message.

    Callback data format:
      deferred_run:<token>
      deferred_discard:<token>

    The token is a per-message random hex string that uniquely identifies the
    deferred entry in iface._deferred_messages.  Using a token instead of
    user_id ensures that an old button cannot accidentally run a newer deferred
    item that has since replaced the one the prompt was sent for: when a newer
    message replaces an older deferred item, the older token is removed, so its
    stale button resolves to "expired".

    Rejected presses (unauthorized user, or a different owner) are answered with
    a private callback alert and never edit the shared prompt message, so they
    cannot destroy the real operator's Run/Discard controls.

    To guard against the operator pressing the button simultaneously from two
    Telegram clients (mobile + desktop), the deferred entry is popped from
    iface._deferred_messages atomically — before any await — so only one
    callback invocation can win the pop; the second sees None and reports
    "expired".
    """
    query = update.callback_query

    data = query.data  # "deferred_run:<token>" or "deferred_discard:<token>"
    parts = data.split(":", 1)
    if len(parts) != 2:
        await _ack_query(query)
        return

    action, token = parts

    caller = query.from_user
    caller_id = caller.id if caller else None

    # Ownership check — purely synchronous, no awaits.
    # Peek at the entry to verify the caller is the owner before touching state.
    peeked = iface._deferred_messages.get(token)
    if peeked is not None and caller_id != peeked.user_id:
        try:
            await query.answer("⛔ This is not your deferred message.", show_alert=True)
        except Exception:
            pass
        return

    # Atomically consume the entry **before** any await so that concurrent
    # callback presses from multiple Telegram clients for the same account
    # (same user_id) cannot both see a non-None deferred and both execute
    # the action.  asyncio is single-threaded but yields at every await, so
    # the pop must happen before the first yield to be atomic.
    # Whichever callback invocation pops first wins; the second gets None and
    # will report "expired" below.
    deferred = iface._deferred_messages.pop(token, None)
    if deferred is not None:
        if iface._current_deferred_token.get(deferred.user_id) == token:
            iface._current_deferred_token.pop(deferred.user_id, None)

    # Dismiss the button spinner now that state has been atomically committed.
    await _ack_query(query)

    if action == "deferred_discard" or deferred is None:
        try:
            await query.edit_message_text(
                "🗑 <b>Deferred message discarded.</b>" if deferred is not None else "⚠️ Deferred message expired.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    # action == "deferred_run"
    try:
        await query.edit_message_text(
            "▶️ <b>Running deferred message…</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    # Re-use the original source_message as the effective message so progress
    # replies thread correctly beneath it.  Build a minimal Update-like wrapper
    # that _run_agent_task can use.
    #
    # We re-enter _run_agent_task which will acquire the per-user lock normally.
    # If the lock is already held again (another message raced in), the deferred
    # run itself will be deferred — handled correctly by the same mechanism.
    class _FakeUpdate:
        """Minimal Update stand-in that provides just what _run_agent_task needs."""
        def __init__(self, message, uid):
            self.effective_message = message
            self.effective_chat = message.chat if hasattr(message, "chat") else message
            self.effective_user = type("_U", (), {"id": uid})()

    fake_update = _FakeUpdate(deferred.source_message, deferred.user_id)
    await iface._run_agent_task(fake_update, ctx, deferred.task_text, deferred.images or None)


@_require_cb_auth
async def cb_subagent_confirm(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Approve / Till /reset / Deny buttons for headless sub-agent sensitive file operations.

    Callback data format:
      subconfirm_yes:<token>
      subconfirm_till_reset:<token>
      subconfirm_no:<token>

    Authorization and same-operator double-press safety mirror cb_deferred:
    - Unauthorized presses get a private alert and do not edit the prompt.
    - The signal method atomically pops the event, so a double-press from
      two Telegram clients (same user_id) only signals once; the second press
      receives an "already resolved" alert.
    """
    query = update.callback_query

    data = query.data  # "subconfirm_yes:<token>" | "subconfirm_no:<token>" | "subconfirm_till_reset:<token>"
    parts = data.split(":", 2)
    if len(parts) < 2:
        await _ack_query(query)
        return

    action = parts[0]
    token = parts[1]
    approved = action != "subconfirm_no"
    is_session = action == "subconfirm_till_reset"

    # Access the shared BuiltinExecutor and depth-0 coordinator through the agent.
    builtin = getattr(getattr(iface, "agent", None), "builtin_executor", None)
    coordinator = getattr(getattr(iface, "agent", None), "_confirmation", None)

    if builtin is None or coordinator is None:
        try:
            await query.answer("⚠️ Executor/coordinator not available.", show_alert=True)
        except Exception:
            pass
        return

    # Grant bookkeeping for sub-agent confirmations before the signal.
    if approved:
        try:
            entry = builtin._pending.get(token)
            tool_name = entry[0] if entry else ""
            zone_path = builtin._zone_paths.get(token, "")
            if tool_name and zone_path:
                from confirmation import GrantLifetime

                grant_dir = os.path.dirname(os.path.realpath(zone_path))
                lifetime = GrantLifetime.SESSION if is_session else GrantLifetime.PROMPT
                scope_owner = builtin._pending_confirmations.scope(token)
                coordinator.grant_ledger.add(
                    tool_name, grant_dir, lifetime, scope_owner=scope_owner,
                )
        except Exception as exc:
            logger.warning(
                "cb_subagent_confirm: grant bookkeeping failed for token=%s: %s",
                token[:8], exc, exc_info=True,
            )

    # Atomically signal the coordinator. Returns False if the token was already
    # resolved or expired (double-press / stale button).
    signalled = coordinator.signal_headless_confirmation(token, approved)

    await _ack_query(query)

    if not signalled:
        try:
            await query.edit_message_text(
                "⚠️ Sub-agent confirmation already resolved or expired.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    if approved:
        if is_session:
            result_text = "✅✅ Approved until /reset — executing…"
        else:
            result_text = "✅ Approved for this run — executing…"
    else:
        result_text = "❌ Denied."
    try:
        await query.edit_message_text(
            f"🤖 <b>Sub-agent confirmation</b>\n\n{result_text}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


@_require_cb_auth
async def cb_oauth_cancel(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the Cancel button on an in-flight OAuth authorization prompt.

    The button always sends the constant ``oauth_cancel:`` (no state appended).
    Any trailing field after ``:`` is extracted only for logging; the MCP manager
    is always signalled unconditionally because only one interactive OAuth flow
    may run at a time.
    """
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":", 1)
    state = parts[1] if len(parts) > 1 else ""
    logger.info("OAuth cancel callback: state=%s", state[:8])

    result = None
    if getattr(iface, "mcp_manager", None):
        result = iface.mcp_manager.cancel_oauth_flow()

    if result and not result.get("success"):
        try:
            await query.answer(
                html.escape(result.get("error", "Unable to cancel OAuth flow.")),
                show_alert=True,
            )
        except Exception:
            pass
        return

    try:
        await query.answer("OAuth flow cancelled.", show_alert=True)
    except Exception:
        pass

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.debug("Could not remove OAuth cancel reply markup: %s", exc)
