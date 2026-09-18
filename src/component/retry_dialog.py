import asyncio
from typing import Callable, Optional

from nicegui import ui, Client
from nicegui.context import context

from ..logger import logger
from ..element.red import RedButton


class RetryAIDialog(ui.dialog):
    """当 AI API 报错（如 503）时提示用户是否重试的弹窗"""

    def __init__(self, error_message: str):
        super().__init__()
        self.props('persistent')

        card_style = 'min-width: 460px; max-width: 640px; border-radius: 12px;'
        with self, ui.card().style(card_style).classes('p-5 flex flex-col gap-3'):
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
                '您可以选择重试，或者放弃 AI 回退到传统正则识别，也可以直接终止当前任务。'
            ).classes('text-xs text-gray-600')

            ui.separator()

            with ui.row().classes('w-full justify-between items-center mt-1'):
                ui.button('终止', on_click=self._handle_abort, color='grey-7').props('outline rounded')
                with ui.row().classes('gap-3'):
                    RedButton('回退正则识别', on_click=self._handle_fallback).props('outline')
                    RedButton('重试 AI 识别', on_click=self._handle_retry)

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
) -> Callable[[str], str]:
    """
    创建可从后台工作线程调用的重试弹窗回调函数。
    阻塞当前工作线程并等待用户在 WebUI 上做出选择（'retry', 'fallback', 'abort'）。
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

    def confirm_retry(error_message: str) -> str:
        if client is None or loop is None or loop.is_closed():
            logger.warning('[AI重试] 无关联客户端或事件循环，跳过弹窗并回退')
            return 'fallback'

        async def _show() -> str:
            try:
                with client:
                    dialog = RetryAIDialog(error_message)
                    result = await dialog
                    try:
                        dialog.delete()
                    except Exception:
                        pass
                    return str(result) if result else 'fallback'
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
