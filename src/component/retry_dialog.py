import json
import asyncio
from typing import Any, Callable, Optional

from nicegui import ui, Client
from nicegui.context import context

from ..logger import logger
from ..element.red import RedButton


class RetryAIDialog(ui.dialog):
    """当 AI API 报错（如 503）时提示用户是否重试的弹窗，支持重试、手动输入响应、回退正则与终止"""

    def __init__(self, error_message: str, prompt: Optional[str] = None):
        super().__init__()
        self.props('persistent')
        self.error_message = error_message
        self.prompt = prompt

        card_style = 'min-width: 480px; max-width: 680px; border-radius: 12px;'
        with self, ui.card().style(card_style).classes('p-5 flex flex-col gap-3'):
            # 模式 1: 错误提示主面板
            self.error_container = ui.column().classes('w-full gap-3')
            with self.error_container:
                with ui.row().classes('items-center gap-2 w-full'):
                    ui.icon('warning', size='md', color='amber-8')
                    ui.label('AI 识别接口异常').style(
                        'font-size: 18px; font-weight: bold; color: #d64d4d;'
                    )

                ui.separator()

                ui.label(
                    'AI 接口在请求时返回了错误。'
                ).classes('text-sm text-gray-700')

                ui.label('接口错误详情：').classes('text-xs font-bold text-gray-500 mt-1')
                with ui.scroll_area().classes(
                    'w-full h-24 bg-gray-50 border border-gray-200 rounded p-2 text-xs text-red-600 font-mono break-all'
                ):
                    ui.label(error_message)

                ui.label(
                    '您可以选择重试，或者手动复制 Prompt 向外部 AI 提问并输入结果，也可以回退到传统正则识别或终止任务。'
                ).classes('text-xs text-gray-600')

                ui.separator()

                with ui.row().classes('w-full justify-between items-center mt-1'):
                    ui.button('终止', on_click=self._handle_abort, color='grey-7').props('outline rounded')
                    with ui.row().classes('gap-2'):
                        RedButton('手动输入', on_click=self._show_manual_mode).props('outline')
                        RedButton('回退正则识别', on_click=self._handle_fallback).props('outline')
                        RedButton('重试 AI 识别', on_click=self._handle_retry)

            # 模式 2: 手动输入模式
            self.manual_container = ui.column().classes('w-full gap-3')
            self.manual_container.set_visibility(False)
            with self.manual_container:
                with ui.row().classes('items-center gap-2 w-full'):
                    ui.icon('content_paste_go', size='md', color='primary')
                    ui.label('手动提供 AI 识别结果').style(
                        'font-size: 18px; font-weight: bold; color: #333;'
                    )

                ui.separator()

                # 上面一个“复制prompt”的按钮
                ui.label('第 1 步：点击下方按钮复制全量 Prompt，发送给外部 AI（如网页端 ChatGPT、Claude 或 Gemini）：').classes('text-xs text-gray-700 font-medium')
                RedButton('复制 Prompt', icon='content_copy', on_click=self._handle_copy_prompt).classes('w-full')

                # 下面一个输入框
                ui.label('第 2 步：将外部 AI 回复的内容（完整 JSON）粘贴在下方输入框中：').classes('text-xs font-bold text-gray-700 mt-1')
                self.manual_input = ui.textarea(
                    placeholder='在此粘贴外部 AI 返回的 JSON 代码块或 JSON 内容...'
                ).classes('w-full font-mono text-xs').props('outlined rows=9')

                ui.separator()

                # 再下面一个“确定”和“取消”按钮
                with ui.row().classes('w-full justify-end items-center gap-3 mt-1'):
                    ui.button('取消', on_click=self._show_error_mode, color='grey-7').props('outline rounded')
                    RedButton('确定', on_click=self._handle_manual_submit)

    def _show_manual_mode(self):
        self.error_container.set_visibility(False)
        self.manual_container.set_visibility(True)

    def _show_error_mode(self):
        self.manual_container.set_visibility(False)
        self.error_container.set_visibility(True)

    async def _handle_copy_prompt(self):
        from ..element.red import notify
        if not self.prompt:
            notify('暂无可复制的 Prompt 内容', type='warning')
            return

        copy_js = f"""
        (function() {{
            const text = {json.dumps(self.prompt)};
            function fallbackCopy(val) {{
                const ta = document.createElement('textarea');
                ta.value = val;
                ta.style.position = 'fixed';
                ta.style.left = '-9999px';
                ta.style.top = '-9999px';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                try {{
                    document.execCommand('copy');
                }} catch (e) {{
                    console.error('execCommand copy failed:', e);
                }} finally {{
                    document.body.removeChild(ta);
                }}
            }}
            if (navigator.clipboard && window.isSecureContext) {{
                navigator.clipboard.writeText(text).catch(function() {{
                    fallbackCopy(text);
                }});
            }} else {{
                fallbackCopy(text);
            }}
        }})();
        """
        try:
            await ui.run_javascript(copy_js)
            notify('全量 Prompt 已成功复制到剪贴板！', type='positive')
        except Exception as e:
            notify(f'复制到剪贴板失败: {e}，请重试', type='negative')

    def _handle_manual_submit(self):
        from ..element.red import notify
        from ..ai.client import AIClient

        val = self.manual_input.value
        if not val or not val.strip():
            notify('输入内容不能为空，请粘贴 AI 返回的 JSON 内容', type='warning')
            return

        try:
            result = AIClient.parse_manual_response(val)
            notify('成功解析手动输入的 AI 结果！', type='positive')
            self.submit(('manual', result))
        except Exception as e:
            notify(f'解析失败: {str(e)}，请检查是否包含符合格式的 JSON 内容', type='negative')

    def _handle_retry(self):
        try:
            with self.client:
                from ..element.red import notify
                notify('正在重试 AI 识别请求，请稍候...', type='info')
        except Exception:
            pass
        self.submit('retry')

    def _handle_fallback(self):
        self.submit('fallback')

    def _handle_abort(self):
        try:
            with self.client:
                from ..element.red import notify
                notify('已选择终止任务', type='warning')
        except Exception:
            pass
        self.submit('abort')


def create_retry_callback(
    client: Optional[Client] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Callable[..., Any]:
    """
    创建可从后台工作线程调用的重试弹窗回调函数。
    阻塞当前工作线程并等待用户在 WebUI 上做出选择（'retry', 'fallback', 'abort', ('manual', result)）。
    """
    try:
        if client is None:
            client = context.client
    except Exception:
        client = None

    try:
        if loop is None:
            loop = asyncio.get_running_loop()
    except Exception:
        loop = None

    def confirm_retry(error_message: str, prompt: Optional[str] = None) -> Any:
        if client is None or loop is None or loop.is_closed():
            logger.warning('[AI重试] 无关联客户端或事件循环，跳过弹窗并回退')
            return 'fallback'

        async def _show() -> Any:
            try:
                with client:
                    dialog = RetryAIDialog(error_message, prompt=prompt)
                    result = await dialog
                    try:
                        dialog.delete()
                    except Exception:
                        pass
                    return result if result else 'fallback'
            except Exception as e:
                logger.error(f'[AI重试] 弹窗展示异常: {e}')
                return 'fallback'

        try:
            future = asyncio.run_coroutine_threadsafe(_show(), loop)
            # 阻塞等待用户在浏览器中选择
            return future.result()
        except Exception as e:
            logger.error(f'[AI重试] 等待用户确认异常: {e}')
            return 'fallback'

    return confirm_retry


class LowConfidenceDialog(ui.dialog):
    """当 AI 置信度不足或匹配可能不符时提示用户选择的弹窗"""

    def __init__(
        self,
        anime_name: str,
        path_name: str,
        confidence: str,
        threshold: str,
        reason: Optional[str] = None,
        extra_notes: Optional[str] = None,
    ):
        super().__init__()
        self.props('persistent')

        card_style = 'min-width: 480px; max-width: 680px; border-radius: 12px;'
        with self, ui.card().style(card_style).classes('p-5 flex flex-col gap-3'):
            with ui.row().classes('items-center gap-2 w-full'):
                ui.icon('warning_amber', size='md', color='orange-8')
                ui.label('AI 置信度不足警告').style(
                    'font-size: 18px; font-weight: bold; color: #d64d4d;'
                )

            ui.separator()

            with ui.column().classes('w-full gap-1'):
                with ui.row().classes('items-center gap-2 text-sm text-gray-800'):
                    ui.label('目标动画:').classes('font-bold text-gray-600')
                    ui.label(anime_name).classes('font-bold text-red-600')

                with ui.row().classes('items-center gap-2 text-sm text-gray-800'):
                    ui.label('待处理项:').classes('font-bold text-gray-600')
                    ui.label(path_name).classes('font-mono text-xs bg-gray-100 p-1 rounded')

                with ui.row().classes('items-center gap-2 text-sm mt-1'):
                    ui.label('置信度评级:').classes('font-bold text-gray-600')
                    ui.badge(confidence, color='negative' if confidence == 'Low' else 'warning')
                    ui.label(f'(设定的生效阈值: {threshold})').classes('text-xs text-gray-500')

            ui.label('AI 分析原因与说明：').classes('text-xs font-bold text-gray-500 mt-1')
            detail_text = reason or 'AI 未能将本地文件与数据库条目建立有效匹配。'
            if extra_notes:
                detail_text += f'\n\n补充说明: {extra_notes}'

            with ui.scroll_area().classes(
                'w-full h-28 bg-orange-50 border border-orange-200 rounded p-2.5 text-xs text-gray-800 font-sans break-words'
            ):
                ui.markdown(detail_text)

            ui.label(
                '建议选择【跳过处理】以免将不匹配的内容（如 OVA、特典或扫图）错误分配进主季。若确定使用文件名提取数字改名，可选择【回退传统正则】。'
            ).classes('text-xs text-gray-600')

            ui.separator()

            with ui.row().classes('w-full justify-between items-center mt-1'):
                ui.button('终止全部任务', on_click=self._handle_abort, color='grey-7').props('outline rounded')
                with ui.row().classes('gap-3'):
                    RedButton('回退传统正则', on_click=self._handle_fallback).props('outline')
                    RedButton('跳过此项处理', on_click=self._handle_skip)

    def _handle_skip(self):
        try:
            with self.client:
                from ..element.red import notify
                notify('已选择跳过当前项目', type='info')
        except Exception:
            pass
        self.submit('skip')

    def _handle_fallback(self):
        try:
            with self.client:
                from ..element.red import notify
                notify('已选择回退到传统正则识别', type='warning')
        except Exception:
            pass
        self.submit('fallback')

    def _handle_abort(self):
        try:
            with self.client:
                from ..element.red import notify
                notify('已选择终止任务', type='negative')
        except Exception:
            pass
        self.submit('abort')


def create_low_confidence_callback(
    client: Optional[Client] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Callable[[str, str, str, str, Optional[str], Optional[str]], str]:
    """
    创建可从后台工作线程调用的低置信度弹窗回调函数。
    阻塞当前工作线程并等待用户在 WebUI 上做出选择（'skip', 'fallback', 'abort'）。
    """
    try:
        if client is None:
            client = context.client
    except Exception:
        client = None

    try:
        if loop is None:
            loop = asyncio.get_running_loop()
    except Exception:
        loop = None

    def confirm_low_confidence(
        anime_name: str,
        path_name: str,
        confidence: str,
        threshold: str,
        reason: Optional[str] = None,
        extra_notes: Optional[str] = None,
    ) -> str:
        if client is None or loop is None or loop.is_closed():
            logger.warning('[AI置信度] 无关联客户端或事件循环，安全起见默认跳过处理')
            return 'skip'

        async def _show() -> str:
            try:
                with client:
                    dialog = LowConfidenceDialog(
                        anime_name=anime_name,
                        path_name=path_name,
                        confidence=confidence,
                        threshold=threshold,
                        reason=reason,
                        extra_notes=extra_notes,
                    )
                    result = await dialog
                    try:
                        dialog.delete()
                    except Exception:
                        pass
                    return str(result) if result else 'skip'
            except Exception as e:
                logger.error(f'[AI置信度] 弹窗展示异常: {e}')
                return 'skip'

        try:
            future = asyncio.run_coroutine_threadsafe(_show(), loop)
            return future.result()
        except Exception as e:
            logger.error(f'[AI置信度] 等待用户确认异常: {e}')
            return 'skip'

    return confirm_low_confidence

