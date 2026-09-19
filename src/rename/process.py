import re
import json
import uuid
from pathlib import Path
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Tuple, Union, Optional

from jikanpy import Jikan

from .trans import Trans
from ..logger import logger
from .get_info import Search
from ..utils.path import TASK_PATH
from .ai_processor import AIProcessor
from ..config.config_manager import cm
from ..exception import TaskAbortedException
from ..ai.models import AIAnalysisResult
from .utils import S0_TAG, EXTRA_TAG, IGNORE_DIR, VIDEO_SUFFIX, IGNORE_SUFFIX
from .cleaner import (
    remove_tag,
    to_sim_max,
    remove_code,
    remove_season,
    divide_by_year,
    extract_number,
    extract_season,
    remove_episode,
    sanitize_name,
    extract_base_num,
    match_and_extract,
    remove_similar_part,
    find_unique_parts_in_videos,
)

jikan = Jikan()


class Rename:
    def __init__(self):
        bgm_path = cm.get_config('bangumi_path') or cm.get_config('anime_path') or '.'
        self.BANGUMI_PATH = Path(bgm_path)
        movie_path = cm.get_config('movie_path') or '.'
        self.MOVIE_PATH = Path(movie_path)
        anime_path = cm.get_config('anime_path') or '.'
        self.ANIME_PATH = Path(anime_path)
        anime_movie_path = cm.get_config('anime_movie_path') or anime_path
        self.ANIME_MOVIE_PATH = Path(anime_movie_path)

        self.ANIME_MOVIE_PATH.mkdir(parents=True, exist_ok=True)
        self.MOVIE_PATH.mkdir(parents=True, exist_ok=True)
        self.ANIME_PATH.mkdir(parents=True, exist_ok=True)
        self.BANGUMI_PATH.mkdir(parents=True, exist_ok=True)
        self.search = Search()
        self.ai_processor = AIProcessor()

        self.R = {}

    def get_season_id(
        self,
        tv_info: Dict,
        work_path: Path,
        path: Path,
        titles: Optional[List[Dict]],
    ):
        season_id = 1
        path_name = path.name
        all_similaritys: List[Dict] = []

        for season in tv_info['seasons']:
            info_season_id = season['season_number']
            target_fold = work_path / f'Season{info_season_id}'
            target_fold.mkdir(parents=True, exist_ok=True)

            sname: str = season['name']
            logger.info(f'[处理任务] Season{info_season_id} 季度名: {sname}')

            '''
            int_season = extract_season(sname)
            logger.info(f'[处理任务] 提取信息季号:{int_season}')
            '''

            int_rtpath_name = extract_season(path_name)
            logger.info(f'[处理任务] 提取标题季号:{int_rtpath_name}')
            if info_season_id == int_rtpath_name:
                season_id = int_rtpath_name
                break

            # 如果不是Season1的情况下，sname处于路径之中，则直接跳过
            if not (sname.strip().startswith('Season') and '1' in sname):
                if sname in path.name:
                    sname_list = sname.split(' ')
                    path_name_list = path.stem.split(' ')
                    if len(sname_list) == len(path_name_list):
                        logger.info(f'[处理任务] 季度名称处于标题中：{sname}')
                        season_id = info_season_id
                        break
                    else:
                        logger.info(f'[处理任务] 季度名称与路径名称长度不同：{sname}')

                if titles:
                    # 或者计算相似度
                    for title in titles:
                        similaritys = {}
                        if title['type'] in [
                            'Default',
                            'Synonym',
                            'English',
                            'French',
                        ]:
                            ename = title['title']
                            path_name = path_name.replace(ename, '')
                            similarity = SequenceMatcher(
                                None,
                                sname,
                                remove_tag(path_name),
                            ).ratio()

                            # logger.debug(f'相似度{tindex}：{similarity}')
                            similaritys[similarity] = season_id
                        all_similaritys.append(similaritys)
        else:
            if all_similaritys:
                logger.info(f'[处理任务] 相似度：{all_similaritys}')
                season_id = to_sim_max(all_similaritys)

        logger.info(f'[处理任务] 识别季号：{season_id}')
        return season_id

    def process_sub(
        self,
        itme_path_main_name: str,
        item_repeat: Optional[List[str]],
        item_path: Path,
        work_path: Path,
        season_id: int,
    ):
        item_name = item_path.name
        if item_repeat:
            item_name_remove = remove_similar_part(item_repeat, item_path.stem)
        else:
            item_name_remove = item_path.stem

        item_name_l = item_name_remove.lower()
        item_suffix = item_path.suffix.lower()

        n_item_name_l = item_name.replace(itme_path_main_name, '').lower()
        logger.info(f'[处理任务] 移去主要内容后的文件名Lower：{n_item_name_l}')

        # 1. 检查是否处于被忽略的文件夹中（例如 Scans, CDs, Fonts 等）
        for ignore_dir in IGNORE_DIR:
            if any(ignore_dir.lower() in part.lower() for part in item_path.parts):
                logger.info(f'[处理任务] 处于忽略目录中，忽略文件：{item_path.name}')
                return

        # 2. 检查后缀是否在忽略后缀中
        for ignore_tag in IGNORE_SUFFIX:
            if ignore_tag.lower() == item_suffix:
                logger.info(f'[处理任务] 忽略文件：{item_path.name}')
                return

        # 3. 严格校验媒体格式：只对视频文件与常见字幕文件处理，其它未知格式一律忽略
        valid_media_suffixes = set(VIDEO_SUFFIX) | {'.ass', '.srt', '.ssa', '.sub', '.vtt'}
        if item_suffix not in valid_media_suffixes:
            logger.info(f'[处理任务] 非视频/字幕媒体文件，忽略：{item_path.name}')
            return

        p = r'[a-zA-Z\u4e00-\u9fa5]'
        for ex in EXTRA_TAG:
            if re.search(
                rf'(?<!{p}){ex.lower()}(?!{p})',
                n_item_name_l,
            ):
                t = work_path / 'extra'
                self.R[item_path] = t / item_name
                logger.info(
                    f'[处理任务] 识别{n_item_name_l},'
                    f'移动到extra文件夹：{item_path.name}'
                )
                break
        else:
            for s0 in S0_TAG:
                if re.search(rf'{s0.lower()}[\d]{{0,3}}', item_name_l):
                    t = work_path / 'Season0'
                    self.R[item_path] = t / item_name
                    logger.info(
                        f'[处理任务] 识别{n_item_name_l},'
                        f'移动到Season0文件夹：{item_path.name}'
                    )
                    break
            else:
                _item_name = remove_code(remove_season(item_name_l))
                logger.info(
                    f'[处理任务] 开始对{_item_name}处理, 寻找集数中...")'
                )
                epp = extract_base_num(_item_name)
                if epp is None:
                    ep = extract_number(_item_name)
                else:
                    ep = int(epp)

                if ep is None:
                    if _item_name.isdigit():
                        ep = int(_item_name)
                    else:
                        season_id = 0
                        ep = 0
                else:
                    ep = int(ep)

                _idata = match_and_extract(item_name)
                if _idata:
                    season_id, ep = _idata[0], _idata[1]

                t = work_path / f'Season{season_id}'

                ep = f'0{ep}' if ep < 10 else ep
                s = f'0{int(season_id)}'
                ss = s if season_id < 10 else int(season_id)
                t.mkdir(parents=True, exist_ok=True)
                ft = f'S{ss}E{ep}'
                self.R[item_path] = t / f'{ft} - {item_name}'
        logger.info(f'[处理任务] 处理完成{item_name}')

    def process(
        self,
        path: Path,
        _is_anime: Optional[bool] = None,
        _is_movie: Optional[bool] = None,
        _tuuid: Optional[str] = None,
        cus_name: Optional[str] = None,
        cus_season_id: Optional[int] = None,
        confirm_retry: Optional[Callable[..., Any]] = None,
        confirm_low_confidence: Optional[Callable[..., str]] = None,
    ):
        if path.is_dir():
            is_video = False
            for sub_path in path.iterdir():
                if not sub_path.is_dir() and sub_path.suffix in VIDEO_SUFFIX:
                    is_video = True

            if is_video:
                return self._process(
                    path,
                    _is_anime,
                    _is_movie,
                    _tuuid,
                    cus_name,
                    cus_season_id,
                    confirm_retry=confirm_retry,
                    confirm_low_confidence=confirm_low_confidence,
                )
            else:
                last_res = None
                for sub_path in path.iterdir():
                    last_res = self._process(
                        sub_path,
                        _is_anime,
                        _is_movie,
                        _tuuid,
                        cus_name,
                        cus_season_id,
                        confirm_retry=confirm_retry,
                        confirm_low_confidence=confirm_low_confidence,
                    )
                    if isinstance(last_res, str) and last_res == "任务已终止":
                        return last_res
                return last_res
        else:
            return self._process(
                path,
                _is_anime,
                _is_movie,
                _tuuid,
                cus_name,
                cus_season_id,
                confirm_retry=confirm_retry,
                confirm_low_confidence=confirm_low_confidence,
            )

    def check_task_type(
        self,
        _uuid: str,
        rtpath_name: str,
        year: int,
        path: Path,
        is_anime: Optional[bool] = None,
        is_movie: Optional[bool] = None,
    ) -> Union[Tuple[str, Dict, bool, bool], str]:
        preferred_source = cm.get_config('anime_info_source') or 'bangumi'
        season_id = extract_season(path.name)
        if season_id == -1:
            season_id = extract_season(rtpath_name)

        # 【首选判断】如果明确是动画，或者未指定且动画首选源为 Bangumi，优先从 Bangumi 检索
        if is_anime is True or (is_anime is None and preferred_source == 'bangumi'):
            logger.info(f'[处理任务] 优先使用 Bangumi 搜索动画: {rtpath_name}')
            b_name, b_info = self.search.search_bangumi(
                query=rtpath_name,
                year=year,
                is_movie=is_movie,
                season_number=season_id if season_id > 1 else 1,
            )
            # 如果带年份没搜到，尝试去除年份再搜一次
            if not b_name and year != 0:
                logger.info(f'[处理任务] Bangumi 带年份未搜索到，尝试去掉年份重试: {rtpath_name}')
                b_name, b_info = self.search.search_bangumi(
                    query=rtpath_name,
                    year=0,
                    is_movie=is_movie,
                    season_number=season_id if season_id > 1 else 1,
                )

            if b_name and b_info:
                detected_is_movie = b_info.get('is_movie', False)
                final_is_movie = is_movie if is_movie is not None else detected_is_movie
                logger.info(
                    f'[处理任务] Bangumi 成功匹配: 《{b_name}》 (类型: {"电影" if final_is_movie else "剧集"})'
                )
                return b_name, b_info, True, final_is_movie
            else:
                logger.info('[处理任务] Bangumi 未检索到对应动画，尝试回退至 TMDB...')

        # 若未从 Bangumi 匹配到，或者为非动画任务，回退使用 TMDB
        if not self.search.TMDB_KEY:
            logger.warning('[处理任务] 未配置 TMDB API 密钥，无法从 TMDB 检索')
            if is_anime:
                return f'[Bangumi/TMDB] 未搜索到动画信息，且未配置 TMDB API Key，跳过 {rtpath_name}'
            return '你还没有配置TMDB的Key！任务失败！请先前往配置界面！'

        season_id = 1
        pos = 0
        logger.info('[处理任务] 开始从 TMDB 判断该文件是否为电视剧/电影！')

        s1_name, s1_info = self.search.get_tv_info(rtpath_name, year)
        logger.info(f'[处理任务] 搜索到的电视剧名称: {s1_name}')
        if not s1_name and year != 0:
            s1_name, s1_info = self.search.get_tv_info(rtpath_name, 0)
            logger.info(f'[处理任务] 未搜索到结果, 删除year后重试: {s1_name}')

        s2_name, s2_info = self.search.get_movie_info(rtpath_name, year)
        logger.info(f'[处理任务] 搜索到的电影名称: {s2_name}')

        if not s2_name and year != 0:
            s2_name, s2_info = self.search.get_movie_info(
                rtpath_name,
                year,
            )
            logger.info(f'[处理任务] 未搜索到结果, 删除year后重试: {s2_name}')

        season_id = extract_season(rtpath_name)

        if s1_name:
            pos += 1
        elif s2_name:
            pos -= 1

        if season_id == -1:
            pos -= 0.6
            if path.is_file():
                pos -= 0.5
        else:
            pos += 0.6
            if path.is_file():
                pos += 0.5

        if path.is_dir():
            path_file_num = len([i for i in path.iterdir() if i.is_file()])
            if path_file_num > 6:
                pos += 0.4
            else:
                pos -= 0.4

        if pos > 0 or (is_movie is not None and not is_movie):
            logger.info('[处理任务] 该文件可能为电视剧！')
            is_movie = False
            info = s1_info
            name = s1_name

            if not info:
                logger.warning(f'[处理任务] 未搜索到电视剧信息, 跳过{rtpath_name}')
                return f'[TMDB] 未搜索到电视剧信息, 跳过{rtpath_name}'

            if is_anime is None:
                for g in info['genres']:
                    if g['name'].lower() == 'animation' or g['name'].lower() == 'anime':
                        is_anime = True
                        break
                else:
                    is_anime = False
        else:
            logger.info('[处理任务] 该文件可能为电影！')
            is_movie = True
            info = s2_info
            name = s2_name

            if not info:
                logger.warning(f'[处理任务] 未搜索到电影信息, 跳过{rtpath_name}')
                return self.error_reply(
                    _uuid,
                    f'[TMDB] 未搜索到电影信息, 跳过{rtpath_name}',
                    path,
                    is_anime,
                )

            if is_anime is None:
                for g in info['genres']:
                    if g['name'].lower() == 'animation' or g['name'].lower() == 'anime':
                        is_anime = True
                        break
                else:
                    is_anime = False
        return name, info, is_anime, is_movie

    def _process(
        self,
        path: Path,
        _is_anime: Optional[bool] = None,
        _is_movie: Optional[bool] = None,
        _tuuid: Optional[str] = None,
        cus_name: Optional[str] = None,
        cus_season_id: Optional[int] = None,
        confirm_retry: Optional[Callable[..., Any]] = None,
        confirm_low_confidence: Optional[Callable[..., str]] = None,
    ):
        if _tuuid:
            _uuid = _tuuid
        else:
            _uuid = str(uuid.uuid4())

        # 检查 TMDB Key (仅当非动画且未配置 TMDB Key 时提示)
        preferred_source = cm.get_config('anime_info_source') or 'bangumi'
        if not self.search.TMDB_KEY and (
            _is_anime is False or (_is_anime is None and preferred_source != 'bangumi')
        ):
            return self.error_reply(
                _uuid,
                '你还没有配置TMDB的Key！任务失败！请先前往配置界面！',
                path,
                _is_anime,
                _is_movie,
            )

        # 【Step.0】 开始处理
        logger.info(f'[处理任务] 开始处理{path.name}')

        # 【Step.1】
        # 先移除无用的标签, 方便之后搜索
        year = 0
        rtpath_name = remove_tag(path.name)
        # 如果标签移除后啥都没有, 说明文件名也是标签的一部分
        if not rtpath_name:
            rtpath_name = remove_tag(path.name, True)
        # 按照空白、换行符或者连字符（-）分割成列表
        path_atri = re.split(r'[\s-]+', rtpath_name)
        # 如果该列表大于3, 不额外处理
        if len(path_atri) > 3:
            # path_atri.pop(0)
            rtpath_name = ' '.join(path_atri)
        # 如果该列表中有多个点, 则认为是一种规范命名的文件
        # 先用.分割之后, 按照年份分割后按照季度分割
        if rtpath_name.count('.') >= 3:
            rtpath_name = ' '.join(rtpath_name.split('.'))
            rtpath_name, year = divide_by_year(rtpath_name)

        rtpath_name = remove_season(rtpath_name)
        rtpath_name = remove_episode(rtpath_name)
        rtpath_name = rtpath_name.strip('!')

        # 如果目录名经过清洗后为空（如纯季名 Season 2），回退使用父级目录名称
        if not rtpath_name.strip() and path.parent and path.parent != path:
            parent_name = remove_tag(path.parent.name)
            parent_name = remove_season(parent_name)
            parent_name = remove_episode(parent_name).strip('!')
            if parent_name:
                rtpath_name = parent_name
                logger.info(f'[处理任务] 目录为季/集标识，回退使用父目录名称: {rtpath_name}')

        logger.info(f'[处理任务] 去除标签后: {rtpath_name}')

        # 如果该路径不是一个视频文件或者不是一个文件夹, 则跳过
        if path.is_file() and path.suffix.lower() not in VIDEO_SUFFIX:
            logger.info(f'[处理任务] {path.name} 不是一个视频文件, 跳过')
            return

        # 【特殊改】
        if cus_name:
            rtpath_name = cus_name

        # 【Step.1.5】
        # 判断类型是否为电影
        task_type = self.check_task_type(
            _uuid,
            rtpath_name,
            year,
            path,
            _is_anime,
            _is_movie,
        )
        if isinstance(task_type, str):
            return self.error_reply(
                _uuid,
                task_type,
                path,
                _is_anime,
                _is_movie,
            )

        name, info, is_anime, is_movie = (
            task_type[0],
            task_type[1],
            task_type[2],
            task_type[3],
        )

        # 【Step.2】
        # 如果是电影
        if is_movie:
            if not name:
                source_label = info.get('source', 'TMDB').upper() if info else 'TMDB'
                logger.warning(f'[处理任务] 未搜索到电影信息, 跳过{rtpath_name}')
                return self.error_reply(
                    _uuid,
                    f'[{source_label}] 未搜索到电影信息, 跳过{rtpath_name}',
                    path,
                    is_anime,
                    is_movie,
                )

            if is_anime:
                _WORK_PATH = self.ANIME_MOVIE_PATH
            else:
                _WORK_PATH = self.MOVIE_PATH

            first_data = (
                info.get('release_date')
                or info.get('first_air_date')
                or info.get('date')
                or '2000-01-01'
            )
            first_year = first_data.split('-')[0]
            work_path = _WORK_PATH / f'{name} ({first_year})'
            work_path.mkdir(parents=True, exist_ok=True)
            if path.is_file():
                self.R[path] = work_path / f'{name} - {path.name}'
            else:
                for item_path in path.iterdir():
                    item_name = item_path.name
                    self.R[item_path] = work_path / f'{name} - {item_name}'
            season_id = 0
        # 如果是剧集类型
        else:
            if is_anime:
                if not name:
                    logger.info('[处理任务] 未搜索到动画! 转为 MyAnimeList 搜索！')
                    search_result = jikan.search(
                        'anime',
                        rtpath_name,
                        page=1,
                    )
                    for i in search_result['data']:
                        if i['type'] == 'Anime':
                            data = i
                            break
                    else:
                        for i in search_result['data']:
                            if i['type'] == 'TV':
                                data = i
                                break
                        else:
                            data = search_result['data'][0]
                    titles = data['titles']
                    logger.info((f'[处理任务] MyAnimeList识别结果: {titles}'))
                else:
                    titles = info.get('titles')
                _WORK_PATH = self.ANIME_PATH
            else:
                titles = [{'type': 'Default', 'title': name}]
                _WORK_PATH = self.BANGUMI_PATH

            if not name:
                source_label = info.get('source', 'TMDB').upper() if info else 'TMDB'
                logger.warning(f'[处理任务] 未搜索到剧集信息, 跳过{rtpath_name}')
                return self.error_reply(
                    _uuid,
                    f'[{source_label}] 未搜索到剧集信息, 跳过{rtpath_name}',
                    path,
                    is_anime,
                    is_movie,
                )

            first_data: str = (
                info.get('first_air_date')
                or info.get('release_date')
                or info.get('date')
                or '2000-01-01'
            )
            first_year = first_data.split('-')[0]

            # 【方案A：TMDB 统一剧集规范】
            # 若该动画为多季剧集（第2季及以上），优先使用追溯得到的主系列第一季名称和首播年份作为大目录名称
            series_name = info.get('series_name') or name
            series_year = info.get('series_year') or first_year
            work_path = _WORK_PATH / f'{sanitize_name(series_name)} ({series_year})'

            season_id = self.get_season_id(
                info,
                work_path,
                path,
                titles,
            )

            if cus_season_id:
                season_id = int(cus_season_id)

            # 若通过 get_season_id 或自定义季号确定了季号大于 1，且此前尚未追溯到 series_name，进行补充追溯并更新 work_path
            if (
                season_id > 1
                and not info.get('series_name')
                and is_anime
                and not is_movie
            ):
                subject_id = info.get('id')
                if subject_id and hasattr(self.search.bangumi, 'get_root_series_info'):
                    try:
                        r_name, r_year = self.search.bangumi.get_root_series_info(subject_id)
                        if r_name:
                            series_name = r_name
                            series_year = r_year or series_year
                            info['series_name'] = series_name
                            info['series_year'] = series_year
                            work_path = _WORK_PATH / f'{sanitize_name(series_name)} ({series_year})'
                            logger.info(
                                f"[处理任务] 补充追溯主系列: 《{name}》 (第{season_id}季) -> 大目录《{series_name} ({series_year})》"
                            )
                    except Exception:
                        pass

            # 【AI增强处理】
            # 如果是动漫且启用了AI，使用AI分析文件映射
            if is_anime and self.ai_processor.ai_client.is_available():
                logger.info("[处理任务] 启用AI分析动漫文件映射")
                logger.info("[处理任务] 填充详细季信息")
                tv_info = self.search.fill_season_info(info)
                try:
                    ai_result: AIAnalysisResult | None = (
                        self.ai_processor.analyze_anime_files(
                            path, tv_info, confirm_retry=confirm_retry
                        )
                    )
                except TaskAbortedException:
                    logger.info(f"[处理任务] 用户主动终止当前任务: {path.name}")
                    return self.error_reply(
                        _uuid,
                        "任务已终止",
                        path,
                        is_anime,
                        is_movie,
                        name,
                        season_id,
                    )

                # 检查AI置信度阈值
                confidence_threshold = cm.get_config("ai_confidence_threshold")
                should_use_ai = False

                if ai_result:
                    if (
                        confidence_threshold == "High"
                        and ai_result.confidence == "High"
                    ):
                        should_use_ai = True
                    elif confidence_threshold == "Medium" and ai_result.confidence in [
                        "High",
                        "Medium",
                    ]:
                        should_use_ai = True
                    elif confidence_threshold == "Low":
                        should_use_ai = True

                if should_use_ai and ai_result:
                    logger.info("[处理任务] 使用AI分析结果进行文件映射")
                    # AI流程独立生成映射，不再需要传统方法预处理
                    self.R = self.ai_processor.apply_ai_mapping(
                        ai_result=ai_result, base_path=path, work_path=work_path
                    )
                    # 如果AI分析成功且路径为文件夹，检查是否存在未匹配的核心视频文件并进行多独立条目级联处理
                    if self.R and path.is_dir():
                        self._process_residual_subjects(
                            path=path,
                            current_info=info,
                            _WORK_PATH=_WORK_PATH,
                            confirm_retry=confirm_retry,
                            confirm_low_confidence=confirm_low_confidence,
                            primary_work_path=work_path,
                        )
                    # 如果AI没有返回任何有效映射，则弹窗询问用户而不是静默回退
                    if not self.R:
                        logger.warning(
                            f"[处理任务] AI未返回任何有效文件映射: {path.name}"
                        )
                        user_choice = "skip"
                        if confirm_low_confidence:
                            user_choice = confirm_low_confidence(
                                anime_name=name,
                                path_name=path.name,
                                confidence=ai_result.confidence,
                                threshold=confidence_threshold,
                                reason=ai_result.reason or "AI分析未生成有效文件映射",
                                extra_notes=ai_result.extra_notes,
                            )
                        if user_choice == "fallback":
                            logger.info("[处理任务] 用户选择回退传统方法处理")
                            self._process_traditional(
                                path, rtpath_name, work_path, season_id
                            )
                        elif user_choice == "abort":
                            logger.info(f"[处理任务] 用户选择终止任务: {path.name}")
                            return self.error_reply(
                                _uuid,
                                "任务已终止",
                                path,
                                is_anime,
                                is_movie,
                                name,
                                season_id,
                            )
                        else:
                            logger.info(f"[处理任务] 跳过未建立有效映射的项目: {path.name}")
                            return self.error_reply(
                                _uuid,
                                f"[AI警告] 未能建立有效映射，已跳过: {path.name}",
                                path,
                                is_anime,
                                is_movie,
                                name,
                                season_id,
                            )
                elif ai_result:
                    logger.info(
                        f"[处理任务] AI置信度不足({ai_result.confidence} < {confidence_threshold}): {path.name}"
                    )
                    user_choice = "skip"
                    if confirm_low_confidence:
                        user_choice = confirm_low_confidence(
                            anime_name=name,
                            path_name=path.name,
                            confidence=ai_result.confidence,
                            threshold=confidence_threshold,
                            reason=ai_result.reason,
                            extra_notes=ai_result.extra_notes,
                        )
                    if user_choice == "fallback":
                        logger.info("[处理任务] 用户选择回退传统方法处理")
                        self._process_traditional(
                            path, rtpath_name, work_path, season_id
                        )
                    elif user_choice == "abort":
                        logger.info(f"[处理任务] 用户选择终止任务: {path.name}")
                        return self.error_reply(
                            _uuid,
                            "任务已终止",
                            path,
                            is_anime,
                            is_movie,
                            name,
                            season_id,
                        )
                    else:
                        logger.info(f"[处理任务] 跳过低置信度项目: {path.name}")
                        return self.error_reply(
                            _uuid,
                            f"[AI置信度不足] 评级为 {ai_result.confidence} (低于阈值 {confidence_threshold})，已跳过",
                            path,
                            is_anime,
                            is_movie,
                            name,
                            season_id,
                        )
                else:
                    logger.info("[处理任务] AI未能返回有效结果（接口报错或用户放弃重试），使用传统方法处理")
                    self._process_traditional(path, rtpath_name, work_path, season_id)
            else:
                # 传统处理方式
                self._process_traditional(path, rtpath_name, work_path, season_id)

        task_path = TASK_PATH / f"{_uuid}.json"
        task_data = {
            "path": str(path),
            "is_anime": is_anime,
            "is_movie": is_movie,
            "name": name,
            "season_id": season_id,
            "uuid": str(_uuid),
            "error": None,
            "use_ai": is_anime and self.ai_processor.ai_client.is_available(),
        }
        trans_result = Trans(self.R, _uuid).trans_file()
        self.R = {}
        if isinstance(trans_result, str):
            return self.error_reply(
                _uuid,
                trans_result,
                path,
                is_anime,
                is_movie,
                name,
                season_id,
            )
        with open(task_path, "w", encoding="UTF-8") as file:
            json.dump(task_data, file, indent=4, ensure_ascii=False)
        return True

    def _process_traditional(
        self, path: Path, rtpath_name: str, work_path: Path, season_id: int
    ):
        """传统处理方式"""
        if path.is_file():
            logger.info(f"[处理任务] 开始对 [单文件] {path.name}处理")
            self.process_sub(
                rtpath_name,
                None,
                path,
                work_path,
                season_id,
            )
        else:
            logger.info(f"[处理任务] 开始对 [文件夹] {path.name}处理")
            repeat = find_unique_parts_in_videos(path)
            for item_path in path.iterdir():
                logger.info(f"[处理任务] 处理嵌套文件夹 {item_path.name}")
                if item_path.is_dir():
                    # 检查是否为忽略的子文件夹（如 Scans, CDs, Fonts 等）
                    if any(ig.lower() in item_path.name.lower() for ig in IGNORE_DIR):
                        logger.info(f"[处理任务] 忽略文件夹：{item_path.name}")
                        continue
                    repeat_2 = find_unique_parts_in_videos(item_path)
                    for sub_item in item_path.iterdir():
                        if sub_item.is_dir():
                            continue
                        self.process_sub(
                            rtpath_name,
                            repeat_2,
                            sub_item,
                            work_path,
                            season_id,
                        )
                else:
                    self.process_sub(
                        rtpath_name,
                        repeat,
                        item_path,
                        work_path,
                        season_id,
                    )

    def _process_residual_subjects(
        self,
        path: Path,
        current_info: Dict[str, Any],
        _WORK_PATH: Path,
        confirm_retry: Optional[Callable[..., Any]] = None,
        confirm_low_confidence: Optional[Callable[..., Any]] = None,
        primary_work_path: Optional[Path] = None,
    ):
        """
        处理单文件夹中包含多个独立条目的情况（级联未匹配文件识别）
        """
        if not path.is_dir() or not self.R:
            return

        all_video_files = self.ai_processor._collect_video_files(path)
        mapped_source_files = {p.resolve() for p in self.R.keys()}
        unmapped_videos = [
            f for f in all_video_files if f.resolve() not in mapped_source_files
        ]

        if not unmapped_videos:
            return

        logger.info(
            f"[多条目识别] 检测到首轮处理后仍有 {len(unmapped_videos)} 个未映射视频文件，检查是否存在多个独立条目..."
        )

        extra_pattern = re.compile(
            r'(?<![a-zA-Z\u4e00-\u9fa5])(ncop|nced|menu|teaser|iv|cm|nc|pv|advice|trailer|event|fans|preview|picture drama|sp|special|特典|映像)(?:\d{1,3})?(?![a-zA-Z\u4e00-\u9fa5])',
            re.IGNORECASE,
        )

        def is_extra_or_sp(f: Path) -> bool:
            # 1. 检查路径是否在 IGNORE_DIR (如 CDs, scans, fonts 等)
            if any(part.lower() in IGNORE_DIR for part in f.parts):
                return True
            # 2. 检查父级目录是否包含 sp, sps, special, specials, extra, extras, 特典 等
            if any(
                p.lower() in ["sp", "sps", "special", "specials", "extra", "extras", "特典"]
                for p in f.parts[:-1]
            ):
                return True
            # 3. 使用独立词/边界正则匹配特典标签 (避免误伤单词内部如 Unlimited, Spice, Charlotte 等)
            if extra_pattern.search(f.name):
                return True
            return False

        def has_core_videos(videos: List[Path]) -> bool:
            for v in videos:
                if not is_extra_or_sp(v):
                    return True
            return False

        current_subject_id = current_info.get("id")
        processed_subject_ids = {current_subject_id} if current_subject_id else set()

        # 记录各目录对应的正片视频
        work_path_core_files: Dict[Path, List[Path]] = {}
        if primary_work_path:
            work_path_core_files[primary_work_path] = [
                f for f in all_video_files
                if f.resolve() in mapped_source_files and not is_extra_or_sp(f)
            ]

        # 检查是否存在尚未映射的核心正片视频
        core_unmapped = [v for v in unmapped_videos if not is_extra_or_sp(v)]

        if core_unmapped:
            logger.info(
                f"[多条目识别] 检测到 {len(core_unmapped)} 个未映射的核心正片视频，尝试识别独立条目..."
            )
            for iteration in range(3):
                if not core_unmapped:
                    break

                matched_subject_info = None
                matched_subject_name = ""

                # 策略1：优先从 Bangumi 动画关联条目中匹配
                if current_info.get("source") == "bangumi" and current_subject_id:
                    relations = self.search.bangumi.get_anime_relations(current_subject_id)
                    avail_relations = [
                        r for r in relations if r.get("id") not in processed_subject_ids
                    ]
                    best_rel = self.search.bangumi.match_relation_by_files(
                        avail_relations, core_unmapped
                    )
                    if best_rel and best_rel.get("id"):
                        rel_id = best_rel["id"]
                        logger.info(
                            f"[多条目识别] 命中关联条目: ID {rel_id} - 《{best_rel.get('name_cn') or best_rel.get('name')}》"
                        )
                        matched_subject_name, matched_subject_info = (
                            self.search.search_bangumi(str(rel_id))
                        )

                # 策略2：若关联条目未命中，使用未匹配核心文件的清洗名称检索 (要求得分 >= 0.70)
                if not matched_subject_name or not matched_subject_info:
                    sample_file = core_unmapped[0]
                    cleaned_q = remove_tag(sample_file.stem)
                    cleaned_q = remove_season(cleaned_q)
                    cleaned_q = remove_episode(cleaned_q).strip("! ")
                    if cleaned_q:
                        logger.info(f"[多条目识别] 尝试使用核心文件清洗名称检索: {cleaned_q}")
                        cand_name, cand_info = self.search.search_bangumi(cleaned_q)
                        if (
                            cand_info
                            and cand_info.get("id") not in processed_subject_ids
                            and cand_info.get("score", 0) >= 0.70
                        ):
                            matched_subject_name = cand_name
                            matched_subject_info = cand_info

                if not matched_subject_name or not matched_subject_info:
                    logger.info("[多条目识别] 未能为剩余核心文件找到高置信度匹配条目，终止级联识别")
                    break

                new_subject_id = matched_subject_info.get("id")
                if new_subject_id:
                    processed_subject_ids.add(new_subject_id)

                first_data = (
                    matched_subject_info.get("first_air_date")
                    or matched_subject_info.get("release_date")
                    or matched_subject_info.get("date")
                    or "2000-01-01"
                )
                first_year = first_data.split("-")[0]
                series_name = matched_subject_info.get("series_name") or matched_subject_name
                series_year = matched_subject_info.get("series_year") or first_year
                sanitized_subject_name = sanitize_name(series_name)
                new_work_path = _WORK_PATH / f"{sanitized_subject_name} ({series_year})"
                if new_work_path != primary_work_path:
                    try:
                        new_work_path.mkdir(parents=True, exist_ok=True)
                    except Exception as e:
                        logger.warning(f"[多条目识别] 预创建目录失败: {str(e)}")
                    logger.info(
                        f"[多条目识别] 为新条目《{matched_subject_name}》建立独立目录: {new_work_path.name}"
                    )
                else:
                    logger.info(
                        f"[多条目识别] 新条目《{matched_subject_name}》属于主系列《{series_name}》，合并入现有目录: {new_work_path.name}"
                    )

                matched_subject_info = self.search.fill_season_info(matched_subject_info)

                try:
                    ai_res = self.ai_processor.analyze_anime_files(
                        path,
                        matched_subject_info,
                        confirm_retry=confirm_retry,
                        video_files=core_unmapped,
                    )
                except Exception as e:
                    logger.warning(f"[多条目识别] AI分析剩余文件失败: {str(e)}")
                    break

                confidence_threshold = (
                    cm.get_config("ai_confidence_threshold") or "Medium"
                )
                should_use_ai = False
                if ai_res:
                    if confidence_threshold == "High" and ai_res.confidence == "High":
                        should_use_ai = True
                    elif confidence_threshold == "Medium" and ai_res.confidence in [
                        "High",
                        "Medium",
                    ]:
                        should_use_ai = True
                    elif confidence_threshold == "Low":
                        should_use_ai = True

                if should_use_ai and ai_res:
                    residual_mapping = self.ai_processor.apply_ai_mapping(
                        ai_result=ai_res, base_path=path, work_path=new_work_path
                    )
                    if residual_mapping:
                        self.R.update(residual_mapping)
                        mapped_source_files.update(
                            p.resolve() for p in residual_mapping.keys()
                        )
                        core_unmapped = [
                            f for f in core_unmapped
                            if f.resolve() not in mapped_source_files
                        ]
                        work_path_core_files.setdefault(new_work_path, []).extend([
                            f for f in residual_mapping.keys()
                            if f in all_video_files and not is_extra_or_sp(f)
                        ])
                        logger.info(
                            f"[多条目识别] 新条目《{matched_subject_name}》成功映射 {len(residual_mapping)} 个文件"
                        )
                    else:
                        break
                else:
                    break

        # 特典/SP 归拢收尾：将所有未映射的 SP 文件直接分配至对应工作目录的 Season0
        mapped_keys_set = set(self.R.keys())
        remaining_sp_videos = [
            f for f in all_video_files if f not in mapped_keys_set
        ]
        if remaining_sp_videos and primary_work_path:
            logger.info(
                f"[多条目识别] 对剩余 {len(remaining_sp_videos)} 个特典/SP文件进行归拢处理..."
            )

            sp_target_dir = primary_work_path / "Season0"
            try:
                sp_target_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"[多条目识别] 预创建特典目录失败: {str(e)}")

            for sp_vid in remaining_sp_videos:
                self.R[sp_vid] = sp_target_dir / sp_vid.name
                logger.info(
                    f"[多条目识别] 归拢特典: {sp_vid.name} -> {primary_work_path.name}/Season0"
                )
                vid_stem = sp_vid.stem
                search_dirs = [sp_vid.parent]
                for sub_name in ["Subs", "subs", "Subtitles", "subtitles"]:
                    sub_dir = sp_vid.parent / sub_name
                    if sub_dir.is_dir():
                        search_dirs.append(sub_dir)

                for d in search_dirs:
                    for item in d.iterdir():
                        if item.is_file() and item != sp_vid and item not in self.R:
                            if item.name.startswith(f"{vid_stem}."):
                                self.R[item] = sp_target_dir / item.name
                                logger.info(
                                    f"[多条目识别] 发现并映射特典关联文件: {item.name}"
                                )

    def error_reply(
        self,
        _uuid: str,
        error: str,
        path: Path,
        is_anime: Optional[bool] = None,
        is_movie: Optional[bool] = None,
        name: Optional[str] = None,
        season_id: Optional[int] = None,
    ):
        task_path = TASK_PATH / f'{_uuid}.json'
        task_data = {
            'path': str(path),
            'is_anime': is_anime,
            'is_movie': is_movie,
            'name': name,
            'season_id': season_id,
            'uuid': str(_uuid),
            'error': error,
        }
        with open(task_path, 'w', encoding='UTF-8') as file:
            json.dump(task_data, file, indent=4, ensure_ascii=False)
        return error
