"""Industry-neutral, user-configured classification and relevance settings."""
from copy import deepcopy
import hashlib
import json
import re
from rules import contains, normalize

STAGES = ['仅宣布', '测试中', '灰度', '已上线', '待确认']
UNCLASSIFIED = '未分类'
DEFAULT_PROFILE = {
    'industry': '', 'context': '',
    'categories': [
        {'name': '产品与服务', 'description': '产品、服务、定价或客户体验的变化', 'keywords': ['product', 'service', '产品', '服务']},
        {'name': '技术与创新', 'description': '研发、技术、研究成果及应用', 'keywords': ['research', 'technology', '技术', '研发']},
        {'name': '市场与经营', 'description': '市场、销售、财务和运营动态', 'keywords': ['earnings', 'sales', 'retail', '营收', '经营']},
        {'name': '政策与监管', 'description': '法律、政策、标准及监管动态', 'keywords': ['regulation', 'legislation', '政策', '监管']},
        {'name': '组织与合作', 'description': '人事、组织、并购、投资与合作', 'keywords': ['partnership', 'acquisition', 'hiring', '合作', '并购']},
        {'name': '风险与事件', 'description': '安全、事故、争议、召回或服务中断', 'keywords': ['recall', 'outage', '事故', '风险']},
        {'name': '其他', 'description': '与业务有关但不属于上述分类的事实', 'keywords': []},
    ],
    'actions': ['持续观察', '深入研究', '内部同步', '无需动作'],
    'priority_keywords': [], 'excluded_keywords': [],
    'regions': [
        {'name': '中国', 'aliases': ['China', '中国']},
        {'name': '东亚', 'aliases': ['East Asia', 'Japan', 'Korea', '日本', '韩国']},
        {'name': '东南亚', 'aliases': ['Southeast Asia', 'Singapore', 'Malaysia', 'Indonesia', 'Vietnam', 'Thailand', 'Philippines', '新加坡', '马来西亚', '印尼', '越南', '泰国', '菲律宾']},
        {'name': '南亚', 'aliases': ['South Asia', 'India', 'Pakistan', '印度', '巴基斯坦']},
        {'name': '欧洲', 'aliases': ['Europe', 'European', 'EU', 'Germany', 'France', 'UK', '德国', '法国', '英国']},
        {'name': '俄罗斯及独联体', 'aliases': ['Russia', 'Russian', 'Kazakhstan', 'Belarus', '俄罗斯', '哈萨克斯坦', '白俄罗斯']},
        {'name': '中东', 'aliases': ['Middle East', 'Saudi', 'UAE', 'Dubai', 'Qatar', '沙特', '阿联酋', '迪拜', '卡塔尔']},
        {'name': '非洲', 'aliases': ['Africa', 'African', 'Kenya', 'Nigeria', 'South Africa', '肯尼亚', '尼日利亚', '南非']},
        {'name': '拉美', 'aliases': ['Latin America', 'Brazil', 'Mexico', 'Argentina', 'Chile', 'Colombia', '巴西', '墨西哥', '阿根廷', '智利', '哥伦比亚']},
        {'name': '北美', 'aliases': ['North America', 'USA', 'United States', 'Canada', '美国', '加拿大']},
        {'name': '大洋洲', 'aliases': ['Oceania', 'Australia', 'New Zealand', '澳大利亚', '新西兰']},
        {'name': '全球', 'aliases': ['global', 'worldwide', '全球']},
        {'name': '其他', 'aliases': []}, {'name': '未涉及', 'aliases': []},
    ],
}


def _text(value, label, limit, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f'{label}必须为文本，长度为 {0 if empty else 1}–{limit} 字符')
    return value.strip()


def _terms(value, label, count=200, limit=100):
    if not isinstance(value, list) or len(value) > count:
        raise ValueError(f'{label}最多 {count} 项')
    return list(dict.fromkeys(_text(v, label, limit, False) for v in value))


def validate_profile(value):
    if not isinstance(value, dict):
        raise ValueError('分析与分类配置必须为对象')
    p = {**deepcopy(DEFAULT_PROFILE), **value}
    p['industry'] = _text(p['industry'], '行业名称', 100)
    p['context'] = _text(p['context'], '业务背景', 6000)
    for key in ('priority_keywords', 'excluded_keywords'):
        p[key] = _terms(p[key], key)
    p['actions'] = _terms(p['actions'], '建议动作', 30)
    if not p['actions']:
        raise ValueError('请至少设置一个建议动作')
    for key, minimum, maximum in [('categories', 1, 50), ('regions', 0, 80)]:
        rows = p[key]
        if key == 'regions' and isinstance(rows,list):
            rows=[r for r in rows if not isinstance(r,dict) or r.get('name') not in ('其他','未涉及')]
        if not isinstance(rows, list) or not minimum <= len(rows) <= maximum:
            raise ValueError(f'{key}数量必须为 {minimum}–{maximum}')
        cleaned = []; names = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(key + '每项必须为对象')
            name = _text(row.get('name'), key + '名称', 80, False)
            if normalize(name) in names:
                raise ValueError(key + '名称不能重复')
            names.add(normalize(name))
            if key == 'categories':
                if name == UNCLASSIFIED:
                    raise ValueError('未分类为系统保留名称')
                cleaned.append(dict(name=name, description=_text(row.get('description', ''), '分类描述', 1000),
                                    keywords=_terms(row.get('keywords', []), '分类关键词')))
            else:
                cleaned.append(dict(name=name, aliases=_terms(row.get('aliases', []), '地区别名')))
        if key == 'regions':
            for name in ('其他', '未涉及'):
                if name not in [row['name'] for row in cleaned]:
                    cleaned.append(dict(name=name, aliases=[]))
        p[key] = cleaned
    return {key: p[key] for key in DEFAULT_PROFILE}


def profile(c):
    return validate_profile(c.get('analysis_profile', {}))


def options(c):
    p = profile(c)
    return dict(categories=[row['name'] for row in p['categories']] + [UNCLASSIFIED],
                actions=p['actions'], stages=list(STAGES))


def region_options(c):
    return [r['name'] for r in profile(c)['regions']]


def signature(c):
    return hashlib.sha256(json.dumps(profile(c), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def matches(text, term):
    # Word boundaries are useful for Latin abbreviations, but would reject a
    # keyword embedded in an ordinary Chinese sentence.
    return normalize(term) in normalize(text) if re.search(r'[\u3400-\u9fff]', term) else contains(text, term)


def classify(text, c):
    ranked = [(sum(matches(text, term) for term in row['keywords']), row['name'])
              for row in profile(c)['categories']]
    best = max((n for n, _ in ranked), default=0)
    winners = [name for n, name in ranked if n == best and n]
    return winners[0] if len(winners) == 1 else UNCLASSIFIED


def excluded(text, c):
    return any(matches(text, term) for term in profile(c)['excluded_keywords'])


def priority(text, c):
    return sum(matches(text, term) for term in profile(c)['priority_keywords'])


def regions(value, c):
    available = profile(c)['regions']
    if value in [r['name'] for r in available]:
        return [value]
    if not value or value in ('待确认', '未知', '未披露'):
        return ['未涉及']
    found = [r['name'] for r in available if any(matches(value, term) for term in [r['name']] + r['aliases'])]
    return found or ['其他']


def prompt(c):
    p = profile(c)
    return ('你是适用于任意行业的新闻事实分析师。先完整分析原文事实，再依据以下业务背景判断相关性；'
            '未设置行业或背景时按通用新闻信息价值判断，不默认任何行业、技术或公司。'
            '分类名称、描述和关键词只用于分类，不是新闻事实或证据，不得据此补充原文没有的内容。'
            '相关性不能替代全文总结；优先词用于排序，排除词用于采集过滤，不应歪曲总结。'
            '阶段采用通用状态：仅宣布=计划/公告；测试中=试验/试点；灰度=分阶段实施；'
            '已上线=已发布/实施/生效；待确认=证据不足。不要把计划或试验写成全面实施。\n'
            + json.dumps(p, ensure_ascii=False, separators=(',', ':')))
