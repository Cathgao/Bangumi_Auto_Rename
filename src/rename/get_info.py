import re
from time import sleep
from typing import Any, Dict, List, Optional

import tmdbsimple as tmdb

from ..logger import logger
from ..config.config_manager import cm
from .bangumi_api import BangumiAPI
from .cleaner import is_chinese_percentage_sufficient


class Search:
    def __init__(self) -> None:
        self.TMDB_KEY = cm.get_config('api_key')
        tmdb.API_KEY = self.TMDB_KEY
        self.bangumi = BangumiAPI()

    def search_bangumi(
        self,
        query: str,
        year: int = 0,
        is_movie: Optional[bool] = None,
        season_number: int = 1,
    ) -> tuple[str, Optional[Dict[str, Any]]]:
        """使用 Bangumi 搜索动画信息，支持英文/罗马音自动通过 TMDB 反查别名"""
        if not query or not query.strip():
            return "", None

        search_query = query.strip()

        # 纯数字 ID 直通查询
        if search_query.isdigit():
            return self.bangumi.search_anime(
                query=search_query,
                year=year,
                is_movie=is_movie,
                season_number=season_number,
            )

        clean_query = re.sub(r'[？!！:：·・/]', ' ', search_query).strip()
        clean_query = re.sub(r'\s+', ' ', clean_query)

        # 1. 尝试直接查询
        name, info = self.bangumi.search_anime(
            query=search_query,
            year=year,
            is_movie=is_movie,
            season_number=season_number,
        )
        if not name and clean_query != search_query:
            name, info = self.bangumi.search_anime(
                query=clean_query,
                year=year,
                is_movie=is_movie,
                season_number=season_number,
            )

        # 如果直接查询匹配度极高(>=0.95)或查询词已为中文且匹配成功，直接返回
        if name and info:
            if is_chinese_percentage_sufficient(search_query) or info.get("score", 0) >= 0.95:
                return name, info

        # 2. 如果非中文（英文/罗马音）或直接查询未匹配到结果，使用 TMDB 反查官方中文/日文标题并精确识别季度
        resolved_candidates: List[Tuple[str, int]] = []
        if not is_chinese_percentage_sufficient(search_query) or not name:
            if self.TMDB_KEY:
                stopwords = {
                    'the', 'a', 'an', 'of', 'and', 'in', 'on', 'at', 'to', 'for', 'with', 'by',
                    'wa', 'ga', 'no', 'wo', 'ni', 'de', 'to', 'ha', 'na', 'mo', 'o', 'ka', 'desu', 'da'
                }
                q_tokens = set(
                    w.lower() for w in re.findall(r'[\w]+', clean_query)
                    if w.lower() not in stopwords and len(w) >= 2
                )

                try:
                    s = tmdb.Search()
                    specific_candidates: List[Tuple[str, int]] = []
                    base_candidates: List[Tuple[str, int]] = []

                    def _search_movie():
                        try:
                            res_m = s.movie(query=clean_query, language="zh-CN")
                            for item in res_m.get("results", [])[:3]:
                                t = item.get("title")
                                ot = item.get("original_title")
                                if t and (t, 1) not in specific_candidates:
                                    specific_candidates.append((t, 1))
                                if ot and (ot, 1) not in specific_candidates:
                                    specific_candidates.append((ot, 1))
                        except Exception:
                            pass

                    tmdb_tv_meta: Dict[str, str] = {}

                    def _search_tv():
                        try:
                            res_tv = s.tv(query=clean_query, language="zh-CN")
                            for item in res_tv.get("results", [])[:3]:
                                tv_id = item.get("id")
                                name_cn = item.get("name", "")
                                name_orig = item.get("original_name", "")
                                if name_cn and not tmdb_tv_meta:
                                    tmdb_tv_meta["series_name"] = name_cn
                                    first_air = item.get("first_air_date", "")
                                    if first_air:
                                        tmdb_tv_meta["series_year"] = first_air.split("-")[0]

                                # 获取季度列表
                                try:
                                    tv = tmdb.TV(tv_id)
                                    info_zh = tv.info(language="zh-CN")
                                    info_en = tv.info()
                                    seasons_zh = {
                                        s.get("season_number"): s.get("name", "")
                                        for s in info_zh.get("seasons", [])
                                    }
                                    seasons_en = {
                                        s.get("season_number"): s.get("name", "")
                                        for s in info_en.get("seasons", [])
                                    }
                                except Exception:
                                    seasons_zh = {}
                                    seasons_en = {}

                                # 计算每个词在各季度中出现的频次（用于逆季度词频 IDF 评分）
                                season_token_counts: Dict[str, int] = {}
                                season_tokens_map: Dict[int, set] = {}
                                for sn in seasons_zh.keys():
                                    if sn == 0:
                                        continue
                                    s_zh = seasons_zh.get(sn, "")
                                    s_en = seasons_en.get(sn, "")
                                    s_text = f"{s_zh} {s_en}".lower()
                                    toks = set(re.findall(r'[\w]+', s_text)) - stopwords
                                    season_tokens_map[sn] = toks
                                    for tok in toks:
                                        season_token_counts[tok] = season_token_counts.get(tok, 0) + 1

                                # 对各季度与查询词进行匹配打分
                                scored_seasons = []
                                for sn, toks in season_tokens_map.items():
                                    score = 0.0
                                    matched_toks = toks.intersection(q_tokens)
                                    for tok in matched_toks:
                                        freq = season_token_counts.get(tok, 1)
                                        score += 10.0 / freq
                                    if (
                                        season_number > 1
                                        and sn == season_number
                                        and score == 0.0
                                    ):
                                        score = 5.0
                                    if score > 0.0:
                                        scored_seasons.append(
                                            (score, sn, seasons_zh.get(sn, ""), seasons_en.get(sn, ""))
                                        )

                                scored_seasons.sort(key=lambda x: x[0], reverse=True)

                                # 将得分最高的特定季度标题作为最优先候选项
                                for score, sn, s_zh, s_en in scored_seasons:
                                    if s_zh and (s_zh, sn) not in specific_candidates:
                                        specific_candidates.append((s_zh, sn))
                                    if s_en and (s_en, sn) not in specific_candidates:
                                        specific_candidates.append((s_en, sn))

                                # 提取查询中未被剧集基础名涵盖的副标题词，组合成新候选项
                                base_text = f"{name_cn} {name_orig}".lower()
                                extra_q_tokens = [t for t in q_tokens if t not in base_text]
                                if extra_q_tokens:
                                    extra_str = " ".join(extra_q_tokens)
                                    if name_cn:
                                        combo_cn = f"{name_cn} {extra_str}"
                                        if (combo_cn, season_number) not in specific_candidates:
                                            specific_candidates.append((combo_cn, season_number))
                                    if name_orig:
                                        combo_orig = f"{name_orig} {extra_str}"
                                        if (combo_orig, season_number) not in specific_candidates:
                                            specific_candidates.append((combo_orig, season_number))

                                # 基础电视剧标题作为兜底候选项
                                if name_cn and (name_cn, season_number) not in base_candidates:
                                    base_candidates.append((name_cn, season_number))
                                if name_orig and (name_orig, season_number) not in base_candidates:
                                    base_candidates.append((name_orig, season_number))
                        except Exception as e:
                            logger.debug(f"[Bangumi 搜索] TMDB TV 检索异常: {str(e)}")

                    if is_movie is True:
                        _search_movie()
                        _search_tv()
                    else:
                        _search_tv()
                        _search_movie()

                    for c in specific_candidates:
                        if c not in resolved_candidates:
                            resolved_candidates.append(c)
                    for c in base_candidates:
                        if c not in resolved_candidates:
                            resolved_candidates.append(c)

                except Exception as e:
                    logger.warning(f"[Bangumi 搜索] TMDB 译名反查失败: {str(e)}")

        if resolved_candidates:
            cand_titles = [c[0] for c in resolved_candidates]
            logger.info(f"[Bangumi 搜索] 英文/罗马音反查候选标题: {cand_titles}")
            for q_res, q_season in resolved_candidates:
                r_name, r_info = self.bangumi.search_anime(
                    query=q_res,
                    year=year,
                    is_movie=is_movie,
                    season_number=q_season,
                )
                if r_name and r_info:
                    if tmdb_tv_meta and not r_info.get("series_name"):
                        r_info["series_name"] = tmdb_tv_meta["series_name"]
                        if "series_year" in tmdb_tv_meta:
                            r_info["series_year"] = tmdb_tv_meta["series_year"]
                    logger.info(
                        f"[Bangumi 搜索] 通过反查标题《{q_res}》成功匹配: 《{r_name}》 (ID: {r_info.get('id')})"
                    )
                    return r_name, r_info

        # 如果反查未获得更优结果，回退使用原始查询结果
        if name and info:
            return name, info

        return "", None

    def get_season_info(
        self, tv_id: int, season_number: int
    ) -> Optional[Dict[str, Any]]:
        """
        获取指定季度的详细信息，包括剧集列表

        Args:
            tv_id: 电视剧ID
            season_number: 季度号

        Returns:
            筛选后的季度信息字典，失败返回None
        """
        for i in range(3):
            try:
                season = tmdb.TV_Seasons(tv_id, season_number)
                season_info = season.info(language="zh-CN")

                if not season_info:
                    logger.warning(f"[季度信息] 未获取到Season {season_number}的信息")
                    return None

                # 筛选季度信息，只保留需要的字段
                filtered_season = {
                    "air_date": season_info.get("air_date"),
                    "episode_count": season_info.get("episode_count", 0),
                    "id": season_info.get("id"),
                    "name": season_info.get("name", ""),
                    "overview": season_info.get("overview", ""),
                    "season_number": season_info.get("season_number", season_number),
                    "episodes": [],
                }

                # 处理剧集信息
                episodes: List[Dict] = season_info.get("episodes", [])
                for episode in episodes:
                    filtered_episode = {
                        "air_date": episode.get("air_date"),
                        "episode_number": episode.get("episode_number"),
                        "episode_type": episode.get("episode_type", "regular"),
                        "name": episode.get("name", ""),
                        "overview": episode.get("overview", ""),
                        "runtime": episode.get("runtime"),
                        "season_number": episode.get("season_number", season_number),
                    }
                    filtered_season["episodes"].append(filtered_episode)

                logger.info(
                    f'[季度信息] 获取Season {season_number}信息成功，包含{len(filtered_season["episodes"])}集'
                )
                return filtered_season

            except Exception as e:
                logger.warning(
                    f"[季度信息] 获取Season {season_number}信息失败，重试第{i + 1}次: {str(e)}"
                )
                sleep(5)

        logger.error(f"[季度信息] 获取Season {season_number}信息最终失败")
        return None

    def get_tv_info_with_seasons(
        self, query: str, year: int
    ) -> tuple[str, Optional[Dict[str, Any]]]:
        """
        获取电视剧信息，包含详细的季度和剧集信息

        Args:
            query: 搜索关键词
            year: 年份

        Returns:
            (剧集名称, 包含详细季度信息的tv_info字典)
        """
        # 首先获取基本的电视剧信息
        name, tv_info = self.get_tv_info(query, year)

        if not name or not tv_info:
            return name, tv_info

        # 填充季度信息
        tv_info = self.fill_season_info(tv_info)
        return name, tv_info

    def fill_season_info(self, tv_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        填充电视剧信息中的季度信息

        Args:
            tv_info: 包含电视剧基本信息的字典

        Returns:
            填充后的tv_info字典
        """
        if not tv_info or "id" not in tv_info:
            logger.error("[季度信息] 无效的电视剧信息，无法填充季度信息")
            return tv_info

        if tv_info.get("source") == "bangumi":
            return self.bangumi.fill_season_info(tv_info)

        tv_id = tv_info["id"]
        seasons = tv_info.get("seasons", [])

        if not seasons:
            logger.warning("[季度信息] 电视剧没有季度信息，尝试获取...")
            # 如果没有季度信息，尝试获取
            name, detailed_tv_info = self.get_tv_info_with_seasons(
                tv_info["name"], tv_info.get("first_air_date", 0)
            )
            if detailed_tv_info:
                return detailed_tv_info
            else:
                logger.error("[季度信息] 获取季度信息失败")
                return tv_info

        # 获取每个季度的详细信息
        for season in seasons:
            season_number = season.get("season_number")
            if season_number is None:
                logger.warning(f"[季度信息] 跳过无效季度: {season}")
                continue

            logger.info(f"[季度信息] 正在获取Season {season_number}的详细信息...")
            detailed_season = self.get_season_info(tv_id, season_number)

            if detailed_season:
                season.update(detailed_season)
            else:
                logger.warning(
                    f"[季度信息] Season {season_number}获取详细信息失败，使用原始数据"
                )

        logger.info(
            f'[季度信息] 电视剧《{tv_info["name"]}》的季度信息填充完成，共{len(seasons)}个季度'
        )
        return tv_info

    def get_movie_info(
        self,
        query: str,
        year: int,
    ):
        for i in range(3):
            try:
                search = tmdb.Search()
                search.movie(
                    query=query,
                    language='zh-CN',
                    year=year if year != 0 else None,
                )
                target_list = search.__dict__['results']
                if target_list:
                    target = target_list[0]
                    name = target['title']
                    movie = tmdb.Movies(target['id'])
                    movie.info()
                    logger.debug(str(movie.__dict__))
                    return name, movie.__dict__
                return '', None
            except:  # noqa:E722, B001
                sleep(5)
                logger.warning(f'[电影搜索] 网络错误, 重试第{i + 1}次中...')
        return '', None

    def get_tv_info(
        self,
        query: str,
        year: int,
    ):
        for i in range(3):
            try:
                for _ in range(3):
                    search = tmdb.Search()
                    search.tv(
                        query=query,
                        language='zh-CN',
                        first_air_date_year=year if year != 0 else None,
                    )
                    target_list = search.__dict__['results']
                    if target_list:
                        target = target_list[0]
                        name = target['name']
                        tv = tmdb.TV(target['id'])
                        tv.info()
                        logger.debug(str(tv.__dict__))
                        return name, tv.__dict__
                    else:
                        if is_chinese_percentage_sufficient(query):
                            query = re.sub(r'[a-zA-Z]', '', query)
                return '', None
            except:  # noqa:E722, B001
                sleep(5)
                logger.warning(f'[电视剧搜索] 网络错误, 重试第{i + 1}次中...')
        return '', None
