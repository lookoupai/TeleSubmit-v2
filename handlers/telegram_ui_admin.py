"""Explicit configuration and reconciliation; no settings mutate on startup."""

import json
from config.settings import ADMIN_IDS, CHANNEL_ID
from database.db_manager import get_db
from utils import runtime_settings
from utils.operation_service import (
    get_operation,
    resolve_publication,
    claim_retry,
    set_operation_state,
)
from utils.paid_ad_service import refund_one_credit
from utils.telegram_capabilities import UI_FEATURES, normalize_pilot_user_ids


async def authorized(update):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("权限不足。")
        return False
    return True


async def ui_settings(update, context):
    if not await authorized(update):
        return
    if context.args:
        feature, *values = context.args
        if feature == "pilot_user_ids" and len(values) == 1:
            try:
                pilot = normalize_pilot_user_ids(values[0])
            except ValueError as exc:
                await update.effective_message.reply_text(str(exc) + " 输入 all 可面向全部用户。")
                return
            await runtime_settings.set_many(
                values={"telegram_ui.pilot_user_ids": pilot}
            )
        elif feature in UI_FEATURES and values in (["on"], ["off"]):
            await runtime_settings.set_many(
                values={"telegram_ui." + feature: str(values[0] == "on").lower()}
            )
        else:
            await update.effective_message.reply_text(
                "用法：/telegram_ui <" + "|".join(UI_FEATURES) + "> on|off\n"
                "/telegram_ui pilot_user_ids <用户ID,用户ID|all>"
            )
            return
    await update.effective_message.reply_text(
        "\n".join(
            f"{label}：{'开启' if runtime_settings.get_bool('telegram_ui.' + key, False) else '关闭'}"
            for key, label in UI_FEATURES.items()
        )
        + "\n试点用户："
        + (runtime_settings.get_raw("telegram_ui.pilot_user_ids") or "全部")
    )


async def publication_status(update, context):
    if not await authorized(update):
        return
    try:
        operation_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("用法：/publish_status <处理编号>")
        return
    op = await get_operation(operation_id)
    if not op:
        await update.effective_message.reply_text("记录不存在。")
        return
    result = json.loads(op["result_json"])
    await update.effective_message.reply_text(
        f"处理编号：{op['id']}\n状态：{op['state']}\n频道：{CHANNEL_ID}\n已记录消息：{result.get('message_ids',[])}\n异常类别：{op['error_code'] or '无'}\n结果未知时请先核对频道，再用 /publish_resolve 明确处理结果。"
    )


async def publication_resolve(update, context):
    if not await authorized(update):
        return
    try:
        operation_id = int(context.args[0])
        mode = context.args[1]
        if mode == "sent" and len(context.args) == 3:
            ids = [int(v) for v in context.args[2].split(",")]
        elif (
            mode == "not_sent"
            and len(context.args) == 3
            and context.args[2] == "confirm"
        ):
            ids = None
        else:
            raise ValueError()
        resolved = await resolve_publication(
            operation_id, update.effective_user.id, message_ids=ids
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "先核对频道。确认已完整发布：/publish_resolve <编号> sent <消息ID,消息ID>\n确认完全未发送：/publish_resolve <编号> not_sent confirm\n已知部分发送不能声明完全未发送。"
        )
        return
    if not resolved:
        await update.effective_message.reply_text(
            "未修改：仅可核对 unknown/partial 操作，并保留已知的消息回执。"
        )
        return
    op = await get_operation(operation_id)
    if ids is None:
        await refund_one_credit(op["owner_user_id"], operation_id=operation_id)
    else:
        from handlers.publish import repair_publication_record

        try:
            repaired = await repair_publication_record(operation_id)
        except Exception:
            repaired = False
        if not repaired:
            await update.effective_message.reply_text(
                "核对结果已保存，内容记录/索引修复未完成，可稍后用 /publish_repair 重试。"
            )
    await update.effective_message.reply_text(
        "已记录人工核对结果：" + op["state"] + "。该操作不会自动重新发送。"
    )


async def publication_repair(update, context):
    if not await authorized(update):
        return
    try:
        operation_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "用法：/publish_repair <编号>；仅修复已确认发布的记录，不发送频道消息。"
        )
        return
    from handlers.publish import repair_publication_record

    try:
        repaired = await repair_publication_record(operation_id)
    except Exception:
        repaired = False
    await update.effective_message.reply_text(
        "记录与索引已修复。" if repaired else "修复未完成，请先确认操作状态或稍后重试。"
    )


async def publication_retry(update, context):
    if not await authorized(update):
        return
    try:
        operation_id = int(context.args[0])
        if context.args[1:] != ["confirm"]:
            raise ValueError()
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "明确重试失败或已知缺失步骤：/publish_retry <编号> confirm\nunknown 必须先核对；重试使用原内容快照。"
        )
        return
    source = await get_operation(operation_id)
    if source and source["state"] == "failed":
        await refund_one_credit(source["owner_user_id"], operation_id=operation_id)
    operation = await claim_retry(operation_id, update.effective_user.id)
    if not operation:
        await update.effective_message.reply_text(
            "未执行：操作尚不允许重试，或已有重试记录。"
        )
        return
    await update.effective_message.reply_text(
        f"已创建重试编号：{operation['id']}。将复用已成功的步骤。"
    )
    from types import SimpleNamespace
    from utils.publish_service import publication_scope
    from handlers.publish import _publish_submission_impl
    from handlers.review_handlers import _publish_approved_submission

    payload = json.loads(operation["payload_json"])
    owner = operation["owner_user_id"]
    try:
        async with publication_scope(operation["id"]):
            if "submission" in payload:
                data = payload["submission"]
                user = SimpleNamespace(
                    id=owner,
                    username=data.get("username"),
                    first_name=data.get("username") or str(owner),
                    last_name=None,
                )
                private_context = SimpleNamespace(
                    bot=context.bot,
                    bot_data=context.bot_data,
                    user_data={
                        "submit_settings_snapshot": payload.get("settings", {}),
                        "paid_ad": payload.get("paid_ad", False),
                        "billing_operation_id": payload.get(
                            "billing_operation_id", operation["id"]
                        ),
                        "publication_review_completed": payload.get(
                            "review_completed", False
                        ),
                    },
                )
                await _publish_submission_impl(
                    SimpleNamespace(effective_user=user),
                    private_context,
                    data,
                    operation["id"],
                    settings=payload.get("settings", {}),
                    paid_ad=payload.get("paid_ad", False),
                )
            else:
                ok, _ = await _publish_approved_submission(
                    context, owner, payload.get("username", str(owner)), payload
                )
                if not ok:
                    raise RuntimeError("Publication incomplete")
                await set_operation_state(operation["id"], "succeeded")
        current = await get_operation(operation["id"])
        if current["state"] == "processing":
            await set_operation_state(operation["id"], "failed")
    except Exception as exc:
        current = await get_operation(operation["id"])
        if current["state"] in ("processing", "sending"):
            await set_operation_state(
                operation["id"],
                "failed" if current["state"] == "processing" else "partial",
                error_code=type(exc).__name__,
            )
    finally:
        current = await get_operation(operation["id"])
        if current["state"] == "failed":
            await refund_one_credit(owner, operation_id=operation["id"])
    await update.effective_message.reply_text(
        f"重试编号：{operation['id']}\n状态：{current['state']}\n请用 /publish_status 查看回执。"
    )
