import re
import json
from typing import Any, Callable, Dict, List, Optional
from pydantic import ValidationError

from ..logger import logger
from .models import AIAnalysisResult
from ..config.config_manager import cm
from .gemini_client import GeminiClient
from .openai_client import OpenAIClient
from .base_client import BaseAIClient


class AIClient:
    """AI客户端工厂类，根据配置选择合适的AI提供商"""

    def __init__(self):
        self.provider = cm.get_config("ai_provider") or "openai"
        self.enabled = bool(cm.get_config("ai_enabled"))
        self.confidence_threshold = cm.get_config("ai_confidence_threshold")

        # 根据提供商创建相应的客户端
        if self.provider.lower() == "gemini":
            self._client: BaseAIClient = GeminiClient()
        else:  # 默认使用OpenAI
            self._client: BaseAIClient = OpenAIClient()

    def is_available(self) -> bool:
        """检查AI客户端是否可用"""
        return bool(self.enabled and self._client and self._client.is_available())

    def analyze_episode_mapping(
        self,
        anime_info: Dict,
        local_files: List[Dict],
        confirm_retry: Optional[Callable[..., Any]] = None,
    ) -> Optional[AIAnalysisResult]:
        """
        分析本地文件与TMDB剧集的映射关系

        Args:
            anime_info: TMDB动漫信息
            local_files: 本地文件信息列表，包含文件名、路径、时长等
            confirm_retry: API报错时的重试确认回调

        Returns:
            验证后的AIAnalysisResult对象
        """
        if not self.is_available():
            logger.warning(f"[AI识别] AI功能未启用或{self.provider}客户端不可用")
            return None

        logger.info(f"[AI识别] 使用 {self.provider.upper()} 进行分析")
        result = self._client.analyze_episode_mapping(
            anime_info, local_files, confirm_retry=confirm_retry
        )

        # 统一在此处保存分析数据
        self._client._save_analysis_data(anime_info, local_files, result)

        return result

    @staticmethod
    def build_common_prompt(anime_info: Dict, local_files: List[Dict]) -> str:
        """
        构建通用的分析提示词，不包含JSON格式要求

        Args:
            anime_info: TMDB动漫信息
            local_files: 本地文件信息列表，包含文件名、路径、时长等

        Returns:
            通用的分析提示词
        """
        # 构建动漫元数据信息
        source_name = anime_info.get('source', 'Bangumi/TMDB').upper()
        meta_info = f"""
动漫名称: {anime_info.get('name', '未知')}
首播日期: {anime_info.get('first_air_date', '未知')}
总季数: {anime_info.get('number_of_seasons', 0)}
总集数: {anime_info.get('number_of_episodes', 0)}
数据来源: {source_name}
"""

        # 构建季度信息
        seasons_info = f"{source_name} 季度与剧集信息：\n"
        seasons_info += json.dumps(anime_info.get("seasons", ""), ensure_ascii=False)

        # 构建本地文件信息
        files_info = "本地文件信息 (路径均为相对路径):\n"
        for i, file_info in enumerate(local_files, 1):
            duration_str = ""
            if file_info.get("duration"):
                duration_str = f" (时长: {file_info['duration']:.1f}分钟)"
            files_info += f"  {file_info['path']}{duration_str}\n"

        prompt = f"""
请分析以下动漫的本地文件与官方数据库({source_name})数据的对应关系：

{meta_info}

{seasons_info}

{files_info}

请特别注意以下常见情况：
0. 第0季通常是特典、SP或OVA集
1. 本地目录可能将多季合并为一个目录，或者相反
2. 本地目录剧集的标号可能会是总集号，而不是官方的分季集号
3. 本地目录可能会给总集篇标注4.5这样的半集号，通常放在第0季
4. OVA/特典可能被放在正片季度末尾，数据库通常会将其放在第0季
5. 本地目录的不同季度可能仅用名称区分，没有明确季号
6. 剧场版有时被混在TV版中，有时被视为单独的电影或特典处理

"""
        return prompt

    @staticmethod
    def get_system_prompt() -> str:
        """
        获取通用的系统提示词

        Returns:
            系统提示词
        """
        return (
            "你是一个专业的动漫文件重命名助手。你需要分析本地动漫文件与动漫数据库中剧集信息的对应关系，特别关注动漫BD发布与官方分季的差异。"
            + "请你只输出匹配到的季度和剧集信息，不要输出其他未匹配到的内容。"
        )

    @staticmethod
    def build_full_prompt(anime_info: Dict, local_files: List[Dict]) -> str:
        """构建用于手动复制给外部AI的完整独立Prompt（包含系统设定、JSON Schema与用户数据）"""
        system_prompt = AIClient.get_system_prompt()
        schema = AIAnalysisResult.model_json_schema()
        schema.pop("title", None)
        schema.pop("description", None)
        schema_str = json.dumps(schema, indent=2, ensure_ascii=False)

        json_instructions = (
            "请严格按照以下JSON Schema格式返回分析结果。不要添加任何额外的解释说明或无关文字，直接输出包含有效JSON的代码块：\n"
            "```json\n"
            f"{schema_str}\n"
            "```"
        )
        base_prompt = AIClient.build_common_prompt(anime_info, local_files)
        return f"{system_prompt}\n\n{json_instructions}\n\n{base_prompt}"

    @staticmethod
    def parse_manual_response(content: str) -> AIAnalysisResult:
        """
        从用户手动粘贴的内容中提取并验证 AIAnalysisResult。
        支持纯 JSON、带 ```json ``` 标记的内容、外部AI思维链以及附加说明文字。
        同时对常见大模型返回的轻微格式差异进行智能兼容与标准化。
        """
        if not content or not content.strip():
            raise ValueError("输入内容为空")

        cleaned = content.strip()

        # 移除可能的思维链标记
        thinking_patterns = [
            r"<thinking>[\s\S]*?</thinking>",
            r"思考：[\s\S]*?(?=\{)",
            r"分析：[\s\S]*?(?=\{)",
            r"推理：[\s\S]*?(?=\{)",
        ]
        for pattern in thinking_patterns:
            cleaned = re.sub(pattern, "", cleaned)

        cleaned = cleaned.strip()

        candidates = []

        # 1. 优先提取 markdown 代码块中的内容
        code_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        for cb in code_blocks:
            cb_clean = cb.strip()
            if cb_clean.startswith("{") and cb_clean.endswith("}"):
                candidates.append(cb_clean)

        # 2. 提取最外层从第一个 { 到最后一个 } 的内容
        first_brace = cleaned.find("{")
        last_brace = cleaned.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            outer = cleaned[first_brace : last_brace + 1].strip()
            if outer not in candidates:
                candidates.append(outer)

        # 3. 原始清洗后的内容
        if cleaned not in candidates:
            candidates.append(cleaned)

        last_json_error = None
        last_validation_error = None

        for cand in candidates:
            try:
                data = json.loads(cand)
            except Exception as e:
                last_json_error = e
                continue

            if not isinstance(data, dict):
                last_json_error = "解析出的 JSON 不是字典对象"
                continue

            # 对常见模型的不规范输出进行智能容错与字段标准化
            try:
                # 兼容 file_mapping 字段名变体
                if "file_mapping" not in data:
                    if "episodes" in data and isinstance(data["episodes"], list):
                        data["file_mapping"] = data.pop("episodes")
                    elif "mapping" in data and isinstance(data["mapping"], list):
                        data["file_mapping"] = data.pop("mapping")

                # 兼容 reason
                if "reason" not in data or not data["reason"]:
                    data["reason"] = "用户手动提供外部 AI 识别结果"

                # 兼容置信度格式
                conf = data.get("confidence")
                if isinstance(conf, str):
                    conf_lower = conf.strip().lower()
                    if conf_lower in ["high", "medium", "low"]:
                        data["confidence"] = conf_lower.capitalize()
                    else:
                        data["confidence"] = "High" if data.get("file_mapping") else "Low"
                elif isinstance(conf, (int, float)):
                    if conf >= 0.8:
                        data["confidence"] = "High"
                    elif conf >= 0.5:
                        data["confidence"] = "Medium"
                    else:
                        data["confidence"] = "Low"
                else:
                    data["confidence"] = "High" if data.get("file_mapping") else "Low"

                # 规范化 file_mapping 中的每一项
                if "file_mapping" in data and isinstance(data["file_mapping"], list):
                    for item in data["file_mapping"]:
                        if isinstance(item, dict):
                            # 兼容文件路径键名
                            if "file_path" not in item:
                                for key in ["path", "original_path", "filename", "file"]:
                                    if key in item and item[key]:
                                        item["file_path"] = item[key]
                                        break
                            # 兼容季号与集号为字符串类型
                            for k in ["tmdb_season", "tmdb_episode"]:
                                if k in item and isinstance(item[k], str) and item[k].isdigit():
                                    item[k] = int(item[k])
                            # 兼容 item 内的 confidence
                            if "confidence" in item and isinstance(item["confidence"], str):
                                c_lower = item["confidence"].strip().lower()
                                if c_lower in ["high", "medium", "low"]:
                                    item["confidence"] = c_lower.capitalize()

                    # 若存在有效的文件映射且不是低置信度，确保置信度为 High，保证直接用作识别信息
                    if data["file_mapping"] and data["confidence"] != "Low":
                        data["confidence"] = "High"

                validated = AIAnalysisResult.model_validate(data)
                return validated
            except ValidationError as ve:
                last_validation_error = ve
            except Exception as e:
                last_validation_error = e

        if last_validation_error:
            raise ValueError(f"JSON 结构验证未通过: {last_validation_error}")
        raise ValueError(f"无法从输入内容中解析有效 JSON: {last_json_error}")


