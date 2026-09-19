import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..logger import logger
from ..config.config_manager import cm
from .cleaner import extract_season, is_chinese_percentage_sufficient


class BangumiAPI:
    """Bangumi (bgm.tv) API 客户端及元数据标准化服务"""

    DEFAULT_BASE_URL = "https://api.bgm.tv"
    USER_AGENT = (
        "Bangumi_Auto_Rename/0.2.1 "
        "(https://github.com/KimigaiiWuyi/Bangumi_Auto_Rename)"
    )

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None:
        self.base_url = (
            base_url
            or cm.get_config("bangumi_base_url")
            or self.DEFAULT_BASE_URL
        ).rstrip("/")
        self.token = token if token is not None else cm.get_config("bangumi_token")
        self._session = requests.Session()

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json",
        }
        token = self.token or cm.get_config("bangumi_token")
        if token:
            headers["Authorization"] = f"Bearer {token.strip()}"
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        retries: int = 3,
        timeout: int = 10,
    ) -> Optional[Any]:
        """统一请求封装，支持失败自动重试"""
        url = f"{self.base_url}{endpoint}"
        for i in range(retries):
            try:
                headers = self._get_headers()
                response = self._session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    headers=headers,
                    timeout=timeout,
                )
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    logger.debug(f"[Bangumi API] 资源未找到: {url}")
                    return None
                elif response.status_code in (429, 502, 503, 504):
                    logger.warning(
                        f"[Bangumi API] 请求限制或服务器繁忙 ({response.status_code})，重试第{i + 1}次..."
                    )
                    time.sleep(2 * (i + 1))
                else:
                    logger.warning(
                        f"[Bangumi API] 请求返回状态码 {response.status_code}: {response.text[:200]}"
                    )
                    time.sleep(1)
            except Exception as e:
                logger.warning(
                    f"[Bangumi API] 网络请求异常，重试第{i + 1}次: {str(e)}"
                )
                time.sleep(2 * (i + 1))

        logger.error(f"[Bangumi API] 请求最终失败: {url}")
        return None

    def search_subjects(
        self,
        keyword: str,
        subject_type: int = 2,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        搜索 Bangumi 条目 (默认为动画 type=2)
        优先使用 /v0/search/subjects，若为空回退旧版 /search/subject/
        """
        if not keyword or not keyword.strip():
            return []

        clean_kw = keyword.strip()
        # 1. 优先尝试 v0 POST 接口
        payload = {
            "keyword": clean_kw,
            "filter": {
                "type": [subject_type],
            },
        }
        res_v0 = self._request(
            "POST",
            "/v0/search/subjects",
            params={"limit": limit},
            json_data=payload,
        )
        if res_v0 and isinstance(res_v0, dict):
            data = res_v0.get("data", [])
            if data:
                return data

        # 2. 回退尝试旧版 GET 接口
        res_legacy = self._request(
            "GET",
            f"/search/subject/{clean_kw}",
            params={"type": subject_type, "responseGroup": "large", "max_results": limit},
        )
        if res_legacy and isinstance(res_legacy, dict):
            list_data = res_legacy.get("list", [])
            if list_data:
                return list_data

        return []

    def get_subject(self, subject_id: int) -> Optional[Dict[str, Any]]:
        """获取条目详情"""
        return self._request("GET", f"/v0/subjects/{subject_id}")

    def get_episodes(
        self,
        subject_id: int,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """获取指定条目的所有章节信息，支持分页遍历"""
        all_episodes: List[Dict[str, Any]] = []
        offset = 0
        max_limit = 100

        while True:
            res = self._request(
                "GET",
                "/v0/episodes",
                params={
                    "subject_id": subject_id,
                    "limit": min(limit, max_limit),
                    "offset": offset,
                },
            )
            if not res or not isinstance(res, dict):
                break

            data = res.get("data", [])
            if not data:
                break

            all_episodes.extend(data)
            total = res.get("total", 0)
            offset += len(data)

            if offset >= total or len(all_episodes) >= 2000:
                break

        return all_episodes

    def get_relations(self, subject_id: int) -> List[Dict[str, Any]]:
        """获取关联条目 (前传、续集、番外篇等)"""
        res = self._request("GET", f"/v0/subjects/{subject_id}/subjects")
        if res and isinstance(res, list):
            return res
        return []

    def get_anime_relations(self, subject_id: int) -> List[Dict[str, Any]]:
        """获取与指定条目关联的动画类条目（排除原声集、书籍等衍生品）"""
        relations = self.get_relations(subject_id)
        anime_relations = []
        for item in relations:
            if not isinstance(item, dict):
                continue
            sub_type = item.get("type")
            rel_type = str(item.get("relation", ""))
            # 过滤明确非动画的条目（如书籍、原声集、三次元、游戏、画集、广播剧）
            if rel_type in ["书籍", "原声集", "三次元", "游戏", "画集", "广播剧"]:
                continue
            if sub_type == 2 or (
                sub_type is None
                and rel_type in ["续集", "前传", "番外篇", "主线故事", "不同演绎", "其他"]
            ):
                anime_relations.append(item)
        return anime_relations

    def get_root_series_info(self, subject_id: int) -> Tuple[str, str]:
        """
        通过 Bangumi 前传关系链，追溯多季动画的主系列第一季名称和首播年份。
        返回: (root_name, root_year)
        若无法追溯或无前传，返回 ("", "")
        """
        if not hasattr(self, "_root_series_cache"):
            self._root_series_cache: Dict[int, Tuple[str, str]] = {}

        if subject_id in self._root_series_cache:
            return self._root_series_cache[subject_id]

        curr_id = subject_id
        visited = {curr_id}
        curr_info = self.get_subject(curr_id)
        if not curr_info:
            return "", ""

        root_info = curr_info
        max_depth = 8
        depth = 0
        while depth < max_depth:
            depth += 1
            relations = self.get_anime_relations(curr_id)
            prequels = [r for r in relations if r.get("relation") == "前传"]
            if not prequels:
                break

            # 优先寻找同属 TV 平台的前传条目
            next_id = None
            next_sub = None
            for p in prequels:
                p_id = p.get("id")
                if not p_id or p_id in visited:
                    continue
                p_sub = self.get_subject(p_id)
                if p_sub and "tv" in str(p_sub.get("platform", "")).lower():
                    next_id = p_id
                    next_sub = p_sub
                    break

            # 若无明确 TV 前传，使用首个未访问的前传
            if not next_id:
                for p in prequels:
                    p_id = p.get("id")
                    if p_id and p_id not in visited:
                        next_id = p_id
                        next_sub = self.get_subject(p_id)
                        break

            if not next_id or not next_sub:
                break

            visited.add(next_id)
            curr_id = next_id
            root_info = next_sub

        root_name = root_info.get("name_cn") or root_info.get("name") or ""
        date_str = root_info.get("date") or ""
        root_year = date_str.split("-")[0] if date_str else ""

        result = (root_name, root_year)
        self._root_series_cache[subject_id] = result
        return result

    def match_relation_by_files(
        self, relations: List[Dict[str, Any]], unmapped_files: List[Path]
    ) -> Optional[Dict[str, Any]]:
        """
        根据未映射文件的文件名特征，在关联条目列表中匹配最符合的条目
        """
        if not relations or not unmapped_files:
            return None

        from .cleaner import remove_tag, remove_season, remove_episode

        clean_stems: List[str] = []
        raw_names: List[str] = []
        for f in unmapped_files:
            raw_names.append(f.name)
            s = remove_tag(f.stem)
            s = remove_season(s)
            s = remove_episode(s).strip("! ")
            if s:
                clean_stems.append(s)

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for rel in relations:
            rel_name = rel.get("name", "")
            rel_name_cn = rel.get("name_cn", "")
            candidates = [rel_name, rel_name_cn]
            score = 0.0

            for name_cand in candidates:
                if not name_cand:
                    continue
                name_cand_lower = name_cand.lower()

                # 特殊符号匹配增强（如 Nachuyachumi!+ 中的 "+"）
                if "+" in name_cand_lower and any("+" in fn for fn in raw_names):
                    score += 0.5

                for stem in clean_stems:
                    stem_lower = stem.lower()
                    if stem_lower in name_cand_lower or name_cand_lower in stem_lower:
                        ratio = len(min(stem_lower, name_cand_lower, key=len)) / max(
                            len(max(stem_lower, name_cand_lower, key=len)), 1
                        )
                        score = max(score, max(ratio, 0.7))
                    else:
                        sim = SequenceMatcher(None, stem_lower, name_cand_lower).ratio()
                        score = max(score, sim)

            if score > 0.35:
                scored.append((score, rel))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_rel = scored[0]
        logger.info(
            f"[Bangumi 关联] 根据未匹配文件成功匹配关联条目: "
            f"《{best_rel.get('name_cn') or best_rel.get('name')}》 (ID: {best_rel.get('id')}, 得分: {best_score:.2f})"
        )
        return best_rel


    @staticmethod
    def _extract_aliases(subject: Dict[str, Any]) -> List[str]:
        """从 infobox 提取别名与中文名"""
        aliases = []
        infobox = subject.get("infobox", [])
        if isinstance(infobox, list):
            for item in infobox:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                val = item.get("value")
                if key in ["中文名", "别名", "英文名", "又名"]:
                    if isinstance(val, str):
                        aliases.append(val)
                    elif isinstance(val, list):
                        for sub_v in val:
                            if isinstance(sub_v, dict) and "v" in sub_v:
                                aliases.append(sub_v["v"])
                            elif isinstance(sub_v, str):
                                aliases.append(sub_v)
        return aliases

    def _calc_similarity(self, query: str, subject: Dict[str, Any]) -> float:
        """计算查询词与条目的最大相似度，惩罚缺少关键副标题词条的候选项"""
        q_lower = query.lower()
        candidates = [
            subject.get("name_cn", ""),
            subject.get("name", ""),
        ]
        candidates.extend(self._extract_aliases(subject))

        stopwords = {
            'the', 'a', 'an', 'of', 'and', 'in', 'on', 'at', 'to', 'for', 'with', 'by',
            'wa', 'ga', 'no', 'wo', 'ni', 'de', 'to', 'ha', 'na', 'mo', 'o', 'ka', 'desu', 'da'
        }
        q_words = [w for w in re.findall(r'[\w]+', q_lower) if w not in stopwords and len(w) >= 2]

        max_ratio = 0.0
        for cand in candidates:
            if not cand:
                continue
            cand_lower = cand.lower()
            if q_lower == cand_lower:
                return 1.0

            cand_words = set(re.findall(r'[\w]+', cand_lower))
            missing_words = [w for w in q_words if w not in cand_words]

            if q_lower in cand_lower or cand_lower in q_lower:
                coverage = len(min(q_lower, cand_lower, key=len)) / max(
                    len(max(q_lower, cand_lower, key=len)), 1
                )
                if coverage >= 0.7 and not missing_words:
                    ratio = max(coverage, 0.85)
                else:
                    ratio = coverage
            else:
                ratio = SequenceMatcher(None, q_lower, cand_lower).ratio()

            # 若候选标题缺少查询中的关键副标题词汇（例如 BLOOM, Nachuyachumi, Alicization 等），进行惩罚
            if missing_words:
                penalty = 0.25 * len(missing_words)
                ratio = max(0.0, ratio - penalty)

            if ratio > max_ratio:
                max_ratio = ratio

        return max_ratio

    def search_anime(
        self,
        query: str,
        year: int = 0,
        is_movie: Optional[bool] = None,
        season_number: int = 1,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        搜索动画并返回最佳匹配 (name, info)

        Args:
            query: 搜索关键词
            year: 年份 (0 表示不限)
            is_movie: 是否为电影 (None 表示自动判断)
            season_number: 期望季号

        Returns:
            (最佳匹配名称, 包含标准化元数据的 info 字典)
        """
        if not query or not query.strip():
            return "", None

        search_query = query.strip()
        # 支持纯数字 ID 直通查询
        if search_query.isdigit():
            subject_id = int(search_query)
            detailed = self.get_subject(subject_id)
            if detailed:
                detected_season = extract_season(
                    detailed.get("name_cn") or detailed.get("name") or ""
                )
                if detected_season <= 0:
                    for alias in self._extract_aliases(detailed):
                        s = extract_season(alias)
                        if s > 0:
                            detected_season = s
                            break
                final_season = (
                    season_number
                    if season_number > 1
                    else (detected_season if detected_season > 0 else 1)
                )
                info = self.build_anime_info(
                    detailed, season_number=final_season, is_movie=is_movie
                )
                name = info["name"]
                if final_season > 1 and not info.get("is_movie"):
                    try:
                        root_name, root_year = self.get_root_series_info(subject_id)
                        if root_name:
                            info["series_name"] = root_name
                            info["series_year"] = root_year
                            logger.info(
                                f"[Bangumi 搜索] 多季动画主系列追溯: 《{name}》 (第{final_season}季) -> 主系列《{root_name}》 ({root_year})"
                            )
                    except Exception as e:
                        logger.debug(f"[Bangumi 搜索] 追溯主系列失败: {str(e)}")
                logger.info(
                    f"[Bangumi 搜索] 数字ID直通匹配: 《{name}》 (ID: {subject_id})"
                )
                return name, info
            else:
                logger.warning(f"[Bangumi 搜索] 数字ID {subject_id} 未查询到有效条目")

        # 如果提供了特定季号 (>1) 且查询词中没有季信息，尝试添加季关键词
        queries_to_try = [search_query]
        if season_number > 1 and extract_season(search_query) == -1:
            cn_nums = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
            cn_season = cn_nums[season_number] if season_number < len(cn_nums) else str(season_number)
            queries_to_try.insert(0, f"{search_query} 第{cn_season}季")
            queries_to_try.insert(1, f"{search_query} Season {season_number}")

        results: List[Dict[str, Any]] = []
        for q in queries_to_try:
            items = self.search_subjects(q, subject_type=2, limit=10)
            if items:
                results = items
                break

        if not results:
            logger.info(f"[Bangumi 搜索] 未找到动画条目: {query}")
            return "", None

        # 评分与筛选
        scored_items: List[Tuple[float, Dict[str, Any]]] = []
        for item in results:
            score = self._calc_similarity(query, item)
            platform = str(item.get("platform", "")).lower()
            is_item_movie = "剧场版" in platform or "movie" in platform

            # 如果用户指定了电影/非电影偏好
            if is_movie is not None:
                if is_movie and is_item_movie:
                    score += 0.2
                elif not is_movie and not is_item_movie:
                    score += 0.2
                else:
                    score -= 0.3

            # 年份匹配奖励
            item_date = item.get("date", "")
            if year != 0 and item_date:
                item_year = item_date.split("-")[0]
                if item_year == str(year):
                    score += 0.15
                else:
                    score -= 0.05

            scored_items.append((score, item))

        scored_items.sort(key=lambda x: x[0], reverse=True)
        best_score, best_item = scored_items[0]

        if best_score < 0.3:
            logger.info(
                f"[Bangumi 搜索] 最佳匹配相似度过低 ({best_score:.2f})，放弃结果: {best_item.get('name')}"
            )
            return "", None

        # 补全条目详情（确保获取完整 infobox、tags、platform 等）
        subject_id = best_item["id"]
        detailed = self.get_subject(subject_id)
        if detailed:
            best_item.update(detailed)

        # 确定季号
        detected_season = extract_season(
            best_item.get("name_cn") or best_item.get("name") or ""
        )
        if detected_season <= 0:
            for alias in self._extract_aliases(best_item):
                s = extract_season(alias)
                if s > 0:
                    detected_season = s
                    break

        final_season = season_number if season_number > 1 else (detected_season if detected_season > 0 else 1)

        info = self.build_anime_info(best_item, season_number=final_season, is_movie=is_movie)
        info["score"] = best_score
        name = info["name"]

        # 当为电视剧且季号大于 1 时，追溯第一季/主系列条目信息供目录归拢
        if final_season > 1 and not info.get("is_movie"):
            try:
                root_name, root_year = self.get_root_series_info(subject_id)
                if root_name:
                    info["series_name"] = root_name
                    info["series_year"] = root_year
                    logger.info(
                        f"[Bangumi 搜索] 多季动画主系列追溯: 《{name}》 (第{final_season}季) -> 主系列《{root_name}》 ({root_year})"
                    )
            except Exception as e:
                logger.debug(f"[Bangumi 搜索] 追溯主系列失败: {str(e)}")

        logger.info(
            f"[Bangumi 搜索] 成功匹配: 《{name}》 (ID: {subject_id}, 得分: {best_score:.2f})"
        )
        return name, info

    def build_anime_info(
        self,
        subject: Dict[str, Any],
        season_number: int = 1,
        is_movie: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        将 Bangumi 条目字典转换为系统标准 info 格式
        兼容 process.py、AIProcessor 以及 AIClient
        """
        name_cn = subject.get("name_cn", "")
        name_orig = subject.get("name", "")
        name = name_cn if name_cn else name_orig
        date_str = subject.get("date") or "2000-01-01"

        platform = str(subject.get("platform", ""))
        is_sub_movie = "剧场版" in platform or "movie" in platform.lower()
        if is_movie is not None:
            final_is_movie = is_movie
        else:
            final_is_movie = is_sub_movie

        # 构建别名列表供匹配
        aliases = self._extract_aliases(subject)
        titles = [{"type": "Default", "title": name}]
        if name_orig and name_orig != name:
            titles.append({"type": "Synonym", "title": name_orig})
        for al in aliases:
            if al not in [name, name_orig]:
                titles.append({"type": "Synonym", "title": al})

        # 提取分类标签
        tags = [{"name": "Animation"}]
        for t in subject.get("tags", [])[:8]:
            t_name = t.get("name") if isinstance(t, dict) else str(t)
            if t_name:
                tags.append({"name": t_name})

        # 预设季度列表骨架
        season_title = (
            f"第{season_number}季"
            if season_number > 1
            else (name_cn or name_orig or "本篇")
        )
        seasons = [
            {
                "id": subject.get("id"),
                "season_number": season_number,
                "name": season_title,
                "episode_count": subject.get("eps") or subject.get("total_episodes") or 0,
                "air_date": date_str,
                "episodes": [],
            }
        ]

        info: Dict[str, Any] = {
            "id": subject.get("id"),
            "name": name,
            "original_name": name_orig,
            "first_air_date": date_str,
            "release_date": date_str,
            "date": date_str,
            "summary": subject.get("summary", ""),
            "overview": subject.get("summary", ""),
            "number_of_seasons": 1,
            "number_of_episodes": subject.get("eps") or subject.get("total_episodes") or 0,
            "genres": tags,
            "source": "bangumi",
            "platform": platform,
            "is_movie": final_is_movie,
            "seasons": seasons,
            "titles": titles,
        }
        return info

    def fill_season_info(self, tv_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        填充 Bangumi 剧集信息中的季与单集信息
        将正片分配至主季度，SP/特典分配至 Season 0
        """
        if not tv_info or tv_info.get("source") != "bangumi" or "id" not in tv_info:
            return tv_info

        subject_id = tv_info["id"]
        episodes = self.get_episodes(subject_id)
        if not episodes:
            logger.warning(f"[Bangumi 季集] 条目 {subject_id} 未获取到章节列表")
            return tv_info

        # 分离正片 (type==0) 和 特典/SP (type!=0)
        regular_eps: List[Dict[str, Any]] = []
        special_eps: List[Dict[str, Any]] = []

        # 确定主季度季号
        main_season_num = 1
        if tv_info.get("seasons"):
            main_season_num = tv_info["seasons"][0].get("season_number", 1)

        for ep in episodes:
            ep_type = ep.get("type", 0)
            # ep.get('ep') 为季内集号；若为空或0则使用 ep.get('sort')
            ep_num = ep.get("ep")
            if ep_num is None or ep_num == 0:
                ep_num = ep.get("sort", 0)

            ep_obj = {
                "air_date": ep.get("airdate") or tv_info.get("date"),
                "episode_number": ep_num,
                "absolute_episode_number": ep.get("sort", ep_num),
                "episode_type": "special" if ep_type != 0 else "regular",
                "name": ep.get("name_cn") or ep.get("name") or f"第{ep_num}话",
                "original_name": ep.get("name", ""),
                "overview": ep.get("desc", ""),
                "runtime": ep.get("duration") or ep.get("duration_seconds"),
                "season_number": 0 if ep_type != 0 else main_season_num,
            }

            if ep_type == 0:
                regular_eps.append(ep_obj)
            else:
                special_eps.append(ep_obj)

        # 构建更新后的 seasons 列表
        updated_seasons: List[Dict[str, Any]] = []
        if special_eps:
            updated_seasons.append(
                {
                    "id": subject_id * 10,
                    "season_number": 0,
                    "name": "特典",
                    "episode_count": len(special_eps),
                    "air_date": tv_info.get("date"),
                    "episodes": special_eps,
                }
            )

        updated_seasons.append(
            {
                "id": subject_id,
                "season_number": main_season_num,
                "name": tv_info.get("name", "本篇"),
                "episode_count": len(regular_eps),
                "air_date": tv_info.get("date"),
                "episodes": regular_eps,
            }
        )

        tv_info["seasons"] = updated_seasons
        tv_info["number_of_seasons"] = len([s for s in updated_seasons if s["season_number"] > 0])
        tv_info["number_of_episodes"] = len(regular_eps)

        logger.info(
            f"[Bangumi 季集] 条目《{tv_info['name']}》章节填充完成: "
            f"共 {len(regular_eps)} 集正片, {len(special_eps)} 集特典"
        )
        return tv_info
