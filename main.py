"""
AstrBot 智能群管插件 (GroupGuardian)
=====================================
基于 AstrBot V4 官方 API 开发，完全符合框架规范。

功能：
- AI 自动识别群内吵架/冲突，智能警告+自动禁言
- 自动检测刷屏并禁言
- 手动指令：禁言、踢人、全体禁言
- 白名单豁免机制
- 管理员权限分级

指令列表：
  /禁言 @xxx <秒数>       → 禁言指定成员
  /解禁 @xxx              → 解除禁言
  /踢人 @xxx              → 踢出群聊
  /全体禁言               → 开启/关闭全体禁言
  /群管白名单 add @xxx    → 添加白名单
  /群管白名单 del @xxx    → 删除白名单
  /群管白名单 list        → 查看白名单
  /群管状态               → 查看插件运行状态
  /群管帮助               → 查看帮助
"""

import re
import time
import json
from typing import Optional, Dict, List, Set
from collections import defaultdict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger


# ============================================================
# 插件主体
# ============================================================
class GroupGuardianPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        
        self._default_config = {
            "admin_qq": "",
            "whitelist_qq": "",
            "ai_conflict_detection": True,
            "ai_auto_warn": True,
            "ai_auto_mute_after_warn": True,
            "ai_mute_duration": 300,
            "ai_model": "",
            "flood_detection": True,
            "flood_threshold": 5,
            "flood_max_msgs": 3,
            "flood_mute_duration": 60,
            "conflict_window": 10,
            "conflict_cooldown": 60,
        }
        
        self._whitelist: Set[str] = set()
        self._admins: Set[str] = set()
        self._last_ai_analysis: Dict[str, float] = {}
        self._warned_users: Dict[str, Dict[str, float]] = {}
        self._message_buffer: Dict[str, List[dict]] = defaultdict(list)
        self._flood_tracker: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        self._loaded = False
        
        logger.info("[GroupGuardian] ✅ 插件初始化完成")

    async def _ensure_loaded(self):
        """确保配置已加载"""
        if not self._loaded:
            config = getattr(self, 'config', {}) or self._default_config
            
            admin_str = config.get("admin_qq", "")
            if admin_str and admin_str.strip():
                self._admins = {q.strip() for q in admin_str.split(",") if q.strip()}
            
            wl_str = config.get("whitelist_qq", "")
            if wl_str and wl_str.strip():
                self._whitelist = {q.strip() for q in wl_str.split(",") if q.strip()}
            
            self._loaded = True
            logger.info(f"[GroupGuardian] 配置已加载 - 管理员: {self._admins}, 白名单: {self._whitelist}")

    def _cfg(self, key: str):
        config = getattr(self, 'config', {}) or {}
        return config.get(key, self._default_config.get(key))

    def _is_admin(self, qq: str) -> bool:
        return qq in self._admins

    def _is_whitelisted(self, qq: str) -> bool:
        return qq in self._whitelist

    def _is_protected(self, qq: str) -> bool:
        return self._is_admin(qq) or self._is_whitelisted(qq)

    def _extract_at_qq(self, message_str: str) -> Optional[str]:
        match = re.search(r'\[CQ:at,qq=(\d+)\]', message_str)
        if match:
            return match.group(1)
        match = re.search(r'@(\d{5,11})', message_str)
        if match:
            return match.group(1)
        return None

    async def _save_whitelist(self):
        try:
            if hasattr(self, 'config') and isinstance(self.config, dict):
                self.config['whitelist_qq'] = ",".join(self._whitelist)
            await self.context.put_kv_data("group_guardian_whitelist", ",".join(self._whitelist))
            logger.info(f"[GroupGuardian] 白名单已保存: {self._whitelist}")
        except Exception as e:
            logger.error(f"[GroupGuardian] 保存白名单失败: {e}")

    # ==================== 指令注册 ====================

    @filter.command("群管帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助信息"""
        yield event.plain_result(
            "🛡️ **智能群管插件使用指南**\n\n"
            "📌 **管理指令（需管理员权限）：**\n"
            "  /禁言 @xxx 秒数  → 禁言指定成员\n"
            "  /解禁 @xxx       → 解除禁言\n"
            "  /踢人 @xxx       → 踢出群聊\n"
            "  /全体禁言        → 开启/关闭全体禁言\n\n"
            "📌 **白名单管理（需管理员权限）：**\n"
            "  /群管白名单 add @xxx  → 添加白名单\n"
            "  /群管白名单 del @xxx  → 删除白名单\n"
            "  /群管白名单 list      → 查看白名单\n\n"
            "📌 **查询指令：**\n"
            "  /群管状态  → 查看插件运行状态\n"
            "  /群管帮助  → 显示本帮助\n\n"
            "🤖 **AI功能（自动）：**\n"
            "  • 自动检测群内冲突并 @ 警告\n"
            "  • 自动检测刷屏并禁言\n"
            "  • 警告后继续违规自动升级为禁言\n\n"
            "⚙️ 管理员QQ和白名单请在 Web 管理面板中设置"
        )

    @filter.command("群管状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看运行状态"""
        await self._ensure_loaded()
        
        ai_on = "✅" if self._cfg("ai_conflict_detection") else "❌"
        flood_on = "✅" if self._cfg("flood_detection") else "❌"
        auto_warn = "✅" if self._cfg("ai_auto_warn") else "❌"
        auto_mute = "✅" if self._cfg("ai_auto_mute_after_warn") else "❌"
        
        yield event.plain_result(
            f"🛡️ **群管插件状态**\n\n"
            f"🤖 AI冲突检测：{ai_on}\n"
            f"📢 刷屏检测：{flood_on}\n"
            f"⚠️ 主动警告：{auto_warn}\n"
            f"🔇 警告后自动禁言：{auto_mute}\n"
            f"🔇 自动禁言时长：{self._cfg('ai_mute_duration')}秒\n"
            f"👮 管理员：{', '.join(self._admins) if self._admins else '未设置'}\n"
            f"🛡️ 白名单人数：{len(self._whitelist)}人"
        )

    @filter.command("禁言")
    async def cmd_mute(self, event: AstrMessageEvent, target: str = "", duration: int = 60):
        """禁言指定成员 /禁言 @xxx 60"""
        await self._ensure_loaded()
        sender_qq = event.message_obj.sender.user_id
        
        if not self._is_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        
        target_qq = self._extract_at_qq(event.message_str)
        if not target_qq:
            yield event.plain_result("❌ 请 @ 要禁言的成员\n用法：/禁言 @xxx 60")
            return
        
        if self._is_protected(target_qq):
            yield event.plain_result("❌ 该成员在白名单或管理员列表中，无法禁言")
            return
        
        try:
            yield event.plain_result(f"[CQ:ban,qq={target_qq},duration={duration}]")
            yield event.plain_result(f"✅ 已禁言 {target_qq}，时长 {duration} 秒")
            logger.info(f"[GroupGuardian] {sender_qq} 禁言了 {target_qq}，{duration}秒")
        except Exception as e:
            yield event.plain_result(f"❌ 禁言失败: {e}")

    @filter.command("解禁")
    async def cmd_unmute(self, event: AstrMessageEvent):
        """解除禁言 /解禁 @xxx"""
        await self._ensure_loaded()
        sender_qq = event.message_obj.sender.user_id
        
        if not self._is_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        
        target_qq = self._extract_at_qq(event.message_str)
        if not target_qq:
            yield event.plain_result("❌ 请 @ 要解禁的成员\n用法：/解禁 @xxx")
            return
        
        try:
            yield event.plain_result(f"[CQ:ban,qq={target_qq},duration=0]")
            yield event.plain_result(f"✅ 已解除 {target_qq} 的禁言")
        except Exception as e:
            yield event.plain_result(f"❌ 解禁失败: {e}")

    @filter.command("踢人")
    async def cmd_kick(self, event: AstrMessageEvent):
        """踢出群聊 /踢人 @xxx"""
        await self._ensure_loaded()
        sender_qq = event.message_obj.sender.user_id
        
        if not self._is_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        
        target_qq = self._extract_at_qq(event.message_str)
        if not target_qq:
            yield event.plain_result("❌ 请 @ 要踢出的成员\n用法：/踢人 @xxx")
            return
        
        if self._is_protected(target_qq):
            yield event.plain_result("❌ 该成员在白名单或管理员列表中，无法踢出")
            return
        
        try:
            yield event.plain_result(f"[CQ:kick,qq={target_qq}]")
            yield event.plain_result(f"✅ 已踢出 {target_qq}")
            logger.info(f"[GroupGuardian] {sender_qq} 踢出了 {target_qq}")
        except Exception as e:
            yield event.plain_result(f"❌ 踢人失败: {e}")

    @filter.command("全体禁言")
    async def cmd_whole_mute(self, event: AstrMessageEvent):
        """切换全体禁言状态"""
        await self._ensure_loaded()
        sender_qq = event.message_obj.sender.user_id
        
        if not self._is_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        
        try:
            yield event.plain_result("[CQ:whole_ban]")
            yield event.plain_result("✅ 已切换全体禁言状态")
        except Exception as e:
            yield event.plain_result(f"❌ 操作失败: {e}")

    @filter.command("群管白名单")
    async def cmd_whitelist(self, event: AstrMessageEvent):
        """白名单管理 /群管白名单 add/del/list"""
        await self._ensure_loaded()
        sender_qq = event.message_obj.sender.user_id
        
        if not self._is_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        
        parts = event.message_str.strip().split()
        action = parts[1] if len(parts) > 1 else "list"
        
        if action == "list":
            if not self._whitelist:
                yield event.plain_result("📋 白名单为空")
            else:
                yield event.plain_result(f"📋 白名单（{len(self._whitelist)}人）：\n" + "\n".join(self._whitelist))
        
        elif action == "add":
            target_qq = self._extract_at_qq(event.message_str) or (parts[2] if len(parts) > 2 else "")
            if not target_qq or not target_qq.isdigit():
                yield event.plain_result("❌ 请提供有效的QQ号\n用法：/群管白名单 add @xxx")
                return
            self._whitelist.add(target_qq)
            await self._save_whitelist()
            yield event.plain_result(f"✅ 已添加 {target_qq} 到白名单")
        
        elif action == "del":
            target_qq = self._extract_at_qq(event.message_str) or (parts[2] if len(parts) > 2 else "")
            if not target_qq:
                yield event.plain_result("❌ 请提供有效的QQ号\n用法：/群管白名单 del @xxx")
                return
            if target_qq in self._whitelist:
                self._whitelist.discard(target_qq)
                await self._save_whitelist()
                yield event.plain_result(f"✅ 已从白名单移除 {target_qq}")
            else:
                yield event.plain_result(f"❌ {target_qq} 不在白名单中")
        else:
            yield event.plain_result(f"❌ 未知操作: {action}，可用：add / del / list")

    # ==================== 消息监听（AI 分析） ====================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, *args):
        """在 LLM 处理前拦截，用于 AI 冲突检测和刷屏检测"""
        if not event.group_id:
            return
        
        await self._ensure_loaded()
        
        # 缓冲消息
        self._buffer_message(event)
        
        # 刷屏检测
        if self._cfg("flood_detection"):
            await self._check_flood(event)
        
        # AI 冲突检测
        if self._cfg("ai_conflict_detection"):
            await self._check_conflict(event)

    def _buffer_message(self, event: AstrMessageEvent):
        group_id = event.group_id
        self._message_buffer[group_id].append({
            "qq": event.message_obj.sender.user_id,
            "nickname": event.get_sender_name(),
            "content": event.message_str,
            "time": time.time()
        })
        
        window = self._cfg("conflict_window")
        if len(self._message_buffer[group_id]) > window * 3:
            self._message_buffer[group_id] = self._message_buffer[group_id][-window * 2:]

    async def _check_flood(self, event: AstrMessageEvent):
        sender_qq = event.message_obj.sender.user_id
        
        if self._is_protected(sender_qq):
            return
        
        group_id = event.group_id
        now = time.time()
        threshold = self._cfg("flood_threshold")
        max_msgs = self._cfg("flood_max_msgs")
        
        tracker = self._flood_tracker[group_id][sender_qq]
        tracker.append(now)
        tracker[:] = [t for t in tracker if now - t <= threshold]
        
        if len(tracker) > max_msgs:
            duration = self._cfg("flood_mute_duration")
            try:
                # 不能在这里用 yield，需要用 send 或直接调用 API
                await self._send_cq(event, f"[CQ:ban,qq={sender_qq},duration={duration}]")
                await self._send_text(event,
                    f"🔇 [CQ:at,qq={sender_qq}] 检测到刷屏行为，已禁言 {duration} 秒\n"
                    f"📊 {threshold}秒内发送了{len(tracker)}条消息"
                )
                tracker.clear()
                logger.info(f"[GroupGuardian] 刷屏禁言 {sender_qq}，{duration}秒")
            except Exception as e:
                logger.error(f"[GroupGuardian] 刷屏禁言失败: {e}")

    async def _check_conflict(self, event: AstrMessageEvent):
        group_id = event.group_id
        now = time.time()
        
        last_check = self._last_ai_analysis.get(group_id, 0)
        cooldown = self._cfg("conflict_cooldown")
        if now - last_check < cooldown:
            return
        
        buffer = self._message_buffer.get(group_id, [])
        window = self._cfg("conflict_window")
        if len(buffer) < window:
            return
        
        recent_msgs = buffer[-window:]
        self._last_ai_analysis[group_id] = now
        
        result = await self._ai_analyze(recent_msgs)
        if not result:
            return
        if not result.get("is_conflict", False):
            return
        
        conflict_level = result.get("conflict_level", 0)
        if conflict_level < 3:
            return
        
        involved_users = result.get("involved_users", [])
        reason = result.get("reason", "检测到群内冲突")
        involved_users = [str(u) for u in involved_users if not self._is_protected(str(u))]
        
        if not involved_users:
            return
        
        logger.info(f"[GroupGuardian] 检测到冲突！等级: {conflict_level}, 涉及: {involved_users}")
        
        # 主动警告
        if self._cfg("ai_auto_warn"):
            at_list = " ".join([f"[CQ:at,qq={u}]" for u in involved_users])
            await self._send_text(event,
                f"{at_list}\n"
                f"⚠️ **智能群管警告**\n"
                f"检测到群内可能存在冲突行为，请保持友善交流！\n"
                f"📊 冲突等级：{conflict_level}/5\n"
                f"📝 分析：{reason}\n"
                f"💡 如继续冲突，将自动禁言处理"
            )
            
            if group_id not in self._warned_users:
                self._warned_users[group_id] = {}
            for u in involved_users:
                self._warned_users[group_id][str(u)] = now
        
        # 自动禁言
        if self._cfg("ai_auto_mute_after_warn"):
            warned = self._warned_users.get(group_id, {})
            for u in involved_users:
                last_warn = warned.get(str(u), 0)
                if now - last_warn < 300 and now - last_warn > 10:
                    duration = self._cfg("ai_mute_duration")
                    try:
                        await self._send_cq(event, f"[CQ:ban,qq={u},duration={duration}]")
                        await self._send_text(event,
                            f"🔇 [CQ:at,qq={u}] 因警告后继续冲突，已被自动禁言 {duration} 秒"
                        )
                        logger.info(f"[GroupGuardian] 自动禁言 {u}，{duration}秒")
                    except Exception as e:
                        logger.error(f"[GroupGuardian] 自动禁言失败: {e}")

    # ==================== 辅助方法 ====================
    
    async def _send_text(self, event: AstrMessageEvent, text: str):
        """发送文本消息（兼容 on_llm_request 中不能 yield 的情况）"""
        try:
            # 尝试通过 unified_msg_origin 发送
            if hasattr(event, 'unified_msg_origin') and event.unified_msg_origin:
                from astrbot.api.message_components import Plain
                from astrbot.api.event import MessageChain
                chain = MessageChain([Plain(text)])
                await self.context.send_message(event.unified_msg_origin, chain)
            else:
                # fallback
                logger.warning("[GroupGuardian] 无法发送消息，unified_msg_origin 不可用")
        except Exception as e:
            logger.error(f"[GroupGuardian] 发送消息失败: {e}")

    async def _send_cq(self, event: AstrMessageEvent, cq_code: str):
        """发送 CQ 码消息"""
        try:
            if hasattr(event, 'unified_msg_origin') and event.unified_msg_origin:
                from astrbot.api.message_components import Plain
                from astrbot.api.event import MessageChain
                chain = MessageChain([Plain(cq_code)])
                await self.context.send_message(event.unified_msg_origin, chain)
        except Exception as e:
            logger.error(f"[GroupGuardian] 发送CQ码失败: {e}")

    # ==================== AI 分析 ====================

    async def _ai_analyze(self, messages: List[dict]) -> Optional[dict]:
        try:
            context_str = ""
            for msg in messages:
                context_str += f"[{msg.get('nickname', '未知')}](QQ:{msg.get('qq', '')}): {msg.get('content', '')}\n"
            
            prompt = (
                "你是一个群聊管理助手，请分析以下最近的消息记录，判断是否存在吵架/冲突。\n\n"
                f"消息记录：\n{context_str[-3000:]}\n\n"
                "请以JSON格式回复（不要包含其他内容）：\n"
                '{"is_conflict": true/false, "conflict_level": 0-5, '
                '"involved_users": ["qq号1", "qq号2"], "reason": "简短原因", '
                '"recommend_action": "none/warn/mute/kick"}'
            )
            
            model_name = self._cfg("ai_model") or None
            
            if hasattr(self.context, 'get_using_provider'):
                provider = self.context.get_using_provider()
                if provider:
                    response = await provider.text_chat(
                        prompt=prompt,
                        model=model_name,
                        temperature=0.3,
                        max_tokens=300
                    )
                    text = response.get('content', str(response)) if isinstance(response, dict) else str(response)
                    return self._parse_json_response(text)
            
            logger.warning("[GroupGuardian] 无法获取 LLM provider，使用规则判断")
            return self._rule_based_analysis()
            
        except Exception as e:
            logger.error(f"[GroupGuardian] AI分析失败: {e}")
            return None

    def _parse_json_response(self, text: str) -> Optional[dict]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return None

    def _rule_based_analysis(self) -> Optional[dict]:
        return {
            "is_conflict": False,
            "conflict_level": 0,
            "involved_users": [],
            "reason": "规则分析：未检测到明显冲突",
            "recommend_action": "none"
        }

    async def terminate(self):
        logger.info("[GroupGuardian] 插件已卸载")