"""Detail-page planning, quality gate, PC/mobile readback and WareEditor-only repair body.

Every route that creates a new product must pass `check_detail_set` (photo route) or
`listing_detail_errors` (generic listing.json routes) before anything is uploaded or published.
Reference/competitor images are never uploadable: they are style analysis only.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

MIN_MODULES = 6
DEFAULT_MAX_MODULES = 8
SKUS_PER_MODULE = 4
OUTPUT_WIDTH = 790
ALLOWED_WIDTHS = (750, 790)
MODULE_SIZE = '1024x1536'
REQUIRED_KINDS = ('hero', 'selling_points', 'sku_options', 'scene', 'structure', 'specs_package', 'aftercare')
EXEMPT_POLICIES = ('user-single-image', 'platform-limit')
KIND_TITLES = {
    'hero': '首屏商品价值', 'selling_points': '核心卖点', 'sku_options': '款式/组合选择',
    'scene': '真实使用场景', 'structure': '结构与细节', 'specs_package': '参数与包装',
    'aftercare': '注意事项与售后说明',
}
# a sku_options module also answers the "pattern / colour / style" requirement
KIND_COVERS = {'sku_options': ('sku_options', 'style_variants')}
FORBIDDEN = ('价格、促销、折扣、秒杀、榜单、销量排名、优惠券、来源店铺名、他牌名称与标识、水印、二维码、'
             '未经证实的认证/功效/材质/尺寸/承重、绝对化用语（最好、第一、正品保证等）')
_BANNED = re.compile(r'[¥￥]|\d\s*元|价格|到手价|券后|折扣|立减|秒杀|限时|促销|特价|包邮|爆款|热销|销量|榜单|排行|排名|'
                     r'第一|NO\.?\s*1|最好|最佳|最强|正品保证|旗舰店|专卖店|官方|专利|认证|二维码|水印', re.I)
_IMG_SRC = re.compile(r'<img\b[^>]*?\b(?:src|data-src)\s*=\s*([\'"])(.*?)\1', re.I | re.S)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def banned_hits(text, reference_terms=()):
    hits = [m.group(0) for m in _BANNED.finditer(text or '')]
    hits += [t for t in reference_terms if t and t in (text or '')]
    return list(dict.fromkeys(hits))


def scan_visible_text(lines, facts, reference_terms=()):
    """Planned on-image text must be promo-free, free of reference identity and fact-backed."""
    facts_text = _text(facts)
    problems = []
    for line in lines:
        hits = banned_hits(line, reference_terms)
        if hits:
            problems.append(f'画面文字含禁止内容 {hits[:3]}：{line[:20]}')
        for number in re.findall(r'\d+(?:\.\d+)?', line):
            if number not in facts_text:
                problems.append(f'画面文字含未确认数字 {number}：{line[:20]}')
    return problems


def _chunk_skus(skus):
    if not skus:
        return [[]]
    count = max(1, -(-len(skus) // SKUS_PER_MODULE))
    size = -(-len(skus) // count)
    return [skus[i:i + size] for i in range(0, len(skus), size)]


def _specs_lines(facts):
    d = facts.get('detail') or {}
    lines = []
    if facts.get('packageLengthMm') and facts.get('packageWidthMm') and facts.get('packageHeightMm'):
        lines.append(f"包装尺寸：{facts['packageLengthMm']}×{facts['packageWidthMm']}×{facts['packageHeightMm']} mm")
    if facts.get('weightKg'):
        lines.append(f"包装重量：{facts['weightKg']} kg")
    for key, label in (('productSizeText', '商品尺寸'), ('material', '材质'), ('packageContents', '包装清单')):
        if d.get(key):
            lines.append(f'{label}：{d[key]}')
    return lines


def build_detail_plan(manifest, root, *, identity_rules, style, reference_terms=()):
    """Deterministic detail-plan.json content; prompts are generated here, never typed by the user."""
    facts = manifest.get('facts') or {}
    terms = list(reference_terms or (manifest.get('reference') or {}).get('bannedTerms') or [])
    if banned_hits(style, terms):
        raise ValueError('风格摘要含价格/促销/他牌或来源店铺标识，请改写成纯版式、配色、卖点结构描述')
    skus = facts.get('skus') or []
    photos_all = list(manifest['sourcePhotos'])
    title = facts.get('title') or manifest['title']
    points = [str(p) for p in (facts.get('sellingPoints') or []) if str(p).strip()]
    detail = facts.get('detail') or {}
    included = [str(x) for x in (facts.get('includedItems') or []) if str(x).strip()]

    def context_for(chunk):
        specs = [s['spec'] for s in (chunk if chunk else skus)]
        return identity_rules + ('用户确认随商品发货的内容：' + '；'.join(included) + '。' if included else '') + '已确认事实（仅供理解，除“画面文字”外不得写入画面）：' + _text(
            {'title': title, 'sellingPoints': points, 'specs': specs}) + '统一风格锚点（仅文字）：' + style
    common = ('竖版2:3详情长图的一个分段，主体清楚，手机缩略图可读，四周保留约8%安全边距，各分段共享配色/光线/材质但'
              '版式各不相同，不要重复首屏标题卡。画面文字必须逐字绘制下面“画面文字”，不得增加任何其它文字、数字、价格、促销、'
              '品牌、店名、水印或二维码；画面文字为空时整张图不出现可读文字。')

    def module(kind, extra_purpose, lines, visual, sku_chunk=None, photos=None):
        return {'kind': kind, 'covers': list(KIND_COVERS.get(kind, (kind,))), 'title': KIND_TITLES[kind],
                'purpose': extra_purpose, 'exactVisibleText': lines,
                'skuKeys': [s['key'] for s in (sku_chunk or [])], 'photos': photos or photos_all,
                'visual': visual}

    modules = [
        module('hero', '首屏：让买家一眼知道商品是什么、核心价值是什么', [title] + points[:1],
               '商品全貌居中，单一主张，标题与商品同屏，不画参数表。'),
        module('selling_points', '用已确认卖点解释购买理由', points[:4],
               '用商品结构/细节特写配合每条卖点，不并列堆字；没有卖点文字时只做外观特写。'),
    ]
    for chunk in _chunk_skus(skus):
        specs = [s['spec'] for s in chunk]
        photos = list(dict.fromkeys(p for s in chunk for p in (s.get('sourcePhotos') or photos_all))) or photos_all
        modules.append(module('sku_options', '展示全部可售款式/组合/颜色/图案，规格名与商品一一对应', specs,
                              '每个规格一个独立展示格，商品只画对应实拍款式，规格名放在对应商品旁，不增删款式。',
                              chunk, photos))
    modules += [
        module('scene', '真实使用场景：让买家看到怎么用、放在哪', [str(detail['sceneText'])] if detail.get('sceneText') else [],
               '只讲使用场景，真实自然的环境，商品仍是主体且与实拍一致，不画参数表。'),
        module('structure', '结构与细节：只展示实拍可见的结构', [str(x) for x in detail.get('structureNotes') or []],
               '多角度/局部特写；看不清的背面与配件不得补画。'),
        module('specs_package', '参数与包装：只写已确认的尺寸重量材质与包装清单', _specs_lines(facts),
               '干净的参数信息版式，仅排版已给出的文字，禁止补充任何未列出的参数。'),
        module('aftercare', '注意事项与售后说明', [str(x) for x in detail.get('careNotes') or []] or
               ['收货后请检查商品完整性', '如有问题请联系店铺客服'],
               '只讲使用提示与服务，不重新画商品英雄图。'),
    ]
    plan_modules = []
    for order, mod in enumerate(modules, 1):
        mod_id = f'detail-{order:02d}-{mod["kind"].replace("_", "")}'
        problems = scan_visible_text(mod['exactVisibleText'], facts, terms)
        if problems:
            raise ValueError('详情文字未通过检查：' + '；'.join(problems))
        text_block = '画面文字（逐字）：' + (' / '.join(mod['exactVisibleText']) or '无') + '。'
        mod.update(id=mod_id, order=order, outputFile=f'详情图/{mod_id}.jpg', size=MODULE_SIZE, forbidden=FORBIDDEN,
                   factsUsed=sorted(k for k in ('title', 'sellingPoints', 'skus', 'packageLengthMm', 'weightKg', 'detail') if facts.get(k)) if mod['kind'] != 'scene' else ['title'],
                   prompt=context_for(mod['skuKeys'] and [s for s in skus if s['key'] in mod['skuKeys']]) + common + f'本段职责：{mod["title"]}——{mod["purpose"]}。构图：{mod["visual"]}' + text_block)
        plan_modules.append(mod)
    return {'schema': 'detail-plan/v1', 'minimum': MIN_MODULES, 'width': OUTPUT_WIDTH, 'requiredKinds': list(REQUIRED_KINDS),
            'allSkuKeys': [s['key'] for s in skus], 'forbidden': FORBIDDEN, 'modules': plan_modules,
            'referencePolicy': '参考链接原图仅用于风格分析，不得作为生成输入或上传素材'}


def normalize_detail_image(source, destination, width=OUTPUT_WIDTH):
    with Image.open(source) as im:
        im = im.convert('RGB')
        if im.width != width:
            im = im.resize((width, round(im.height * width / im.width)), Image.Resampling.LANCZOS)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        im.save(destination, 'JPEG', quality=92)
    return Path(destination)


def overview_image(paths, destination, thumb=200):
    thumbs = []
    for p in paths:
        with Image.open(p) as im:
            im = im.convert('RGB')
            thumbs.append(im.resize((thumb, round(im.height * thumb / im.width)), Image.Resampling.LANCZOS))
    if not thumbs:
        raise ValueError('No detail images for overview')
    sheet = Image.new('RGB', ((thumb + 8) * len(thumbs) + 8, max(t.height for t in thumbs) + 16), 'white')
    for i, t in enumerate(thumbs):
        sheet.paste(t, (8 + i * (thumb + 8), 8))
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, 'JPEG', quality=85)
    return Path(destination)


def _reference_hashes(root):
    path = Path(root) / '.reference-cache' / 'assets.json'
    if not path.is_file():
        return set()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    return {e.get('sha256') for e in data.get('entries') or [] if e.get('sha256')}


def exemption(manifest):
    policy = ((manifest.get('facts') or {}).get('detailPolicy')) or {}
    if policy.get('type') in EXEMPT_POLICIES and str(policy.get('reason') or '').strip():
        return {'type': policy['type'], 'reason': str(policy['reason']).strip(),
                'minimum': max(1, int(policy.get('minimum') or 1))}
    return None


def check_detail_set(manifest, root, plan=None):
    """Hard pre-publish gate for the photo route. Returns {'ok','problems','modules',...}."""
    from image_quality_gate import duplicate_details
    root = Path(root).resolve()
    problems = []
    items = manifest.get('detailImages') or []
    exempt = exemption(manifest)
    plan = plan if plan is not None else _load_plan(root)
    minimum = exempt['minimum'] if exempt else MIN_MODULES
    if len(items) < minimum:
        problems.append(f'详情图只有{len(items)}张，默认至少{MIN_MODULES}张（用户明确只要少量或平台限制需在 facts.detailPolicy 写明原因）')
    if not exempt:
        if not plan:
            problems.append('缺少 detail-plan.json，请先 make-plan')
        else:
            modules = {m['id']: m for m in plan.get('modules') or []}
            ids = [i.get('id') for i in items]
            if ids != [m['id'] for m in plan.get('modules') or []]:
                problems.append('详情图与 detail-plan 模块不一致或顺序错误')
            kinds = {c for m in plan.get('modules') or [] for c in m.get('covers') or [m['kind']]}
            for kind in REQUIRED_KINDS:
                if kind not in kinds:
                    problems.append(f'缺少必需详情模块：{KIND_TITLES[kind]}')
            if 'style_variants' not in kinds:
                problems.append('缺少图案/颜色/款式模块')
            facts_keys = {s['key'] for s in (manifest.get('facts') or {}).get('skus') or []}
            covered = {k for m in plan.get('modules') or [] if m['kind'] == 'sku_options' for k in m.get('skuKeys') or []}
            if facts_keys - covered:
                problems.append('详情页未覆盖全部SKU：' + ','.join(sorted(facts_keys - covered)))
            if len(plan.get('modules') or []) > max(DEFAULT_MAX_MODULES, len(REQUIRED_KINDS) + len(_chunk_skus(list(facts_keys)))) + 4:
                problems.append('详情模块数量异常过多')
            terms = (manifest.get('reference') or {}).get('bannedTerms') or []
            for mod in modules.values():
                problems += scan_visible_text(mod.get('exactVisibleText') or [], manifest.get('facts') or {}, terms)
    reference = _reference_hashes(root)
    seen_paths, existing = [], []
    for item in items:
        rel = str(item.get('localPath') or '')
        target = (root / rel).resolve()
        if root not in target.parents or '.reference-cache' in target.parts:
            problems.append(f'详情图路径不合法或来自参考缓存：{rel}')
            continue
        try:
            with Image.open(target) as im:
                im.load()
                width, height = im.size
        except Exception:
            problems.append(f'详情图不可读：{rel}')
            continue
        if width not in ALLOWED_WIDTHS:
            problems.append(f'详情图宽度必须为750或790：{rel} 当前{width}')
        if not exempt and height <= width:
            problems.append(f'详情图必须为竖版：{rel}')
        digest = _sha(target)
        if digest in reference:
            problems.append(f'详情图与参考链接原图相同，禁止上传：{rel}')
        if item.get('sha256') and item['sha256'] != digest:
            problems.append(f'详情图文件与记录不一致：{rel}')
        existing.append(str(target))
    if len(set(existing)) != len(existing):
        problems.append('详情图重复')
    hashes = [_sha(p) for p in existing]
    if len(set(hashes)) != len(hashes):
        problems.append('详情图内容完全重复')
    elif len(existing) > 1 and duplicate_details(existing, threshold=2):
        problems.append('详情图高度相似疑似重复')
    return {'ok': not problems, 'problems': list(dict.fromkeys(problems)), 'count': len(items),
            'modules': [{'id': i.get('id'), 'kind': i.get('module'), 'file': i.get('localPath'), 'skuKeys': i.get('skuKeys') or []} for i in items],
            'exemption': exempt}


def _load_plan(root):
    path = Path(root) / 'detail-plan.json'
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else None


def listing_detail_errors(images, product):
    """Generic gate for listing.json routes (link move / replica / matrix / templates)."""
    detail = [i for i in images if i.get('role') == 'detail']
    if not images:
        return []
    policy = product.get('detailPolicy')
    if policy in EXEMPT_POLICIES:
        return [] if str(product.get('detailExemptionReason') or '').strip() else ['详情页例外必须写明 detailExemptionReason']
    if len(detail) < MIN_MODULES:
        return [f'详情图只有{len(detail)}张，默认至少{MIN_MODULES}张；请补齐完整详情页，或由用户明确只要少量并记录 detailPolicy/detailExemptionReason']
    return []


def normalize_url(url):
    url = str(url or '').strip()
    if '/imgzone/' in url:
        return url.split('/imgzone/', 1)[1].split('?', 1)[0].lstrip('/')
    parts = urlsplit(url)
    return (parts.netloc + parts.path) if parts.netloc else url.split('?', 1)[0].lstrip('/')


def extract_image_urls(html):
    return [m.group(2) for m in _IMG_SRC.finditer(html or '')]


def detail_html(urls, mobile=False):
    style = 'max-width:100%;' if mobile else 'max-width:790px;'
    return ''.join(f"<p><img style='{style}' src='{u}'/></p>" for u in urls)


def readback_detail(product_detail_desc, expected_urls):
    """Compare remote PC and mobile detail HTML to the local plan (count + order)."""
    desc = product_detail_desc if isinstance(product_detail_desc, dict) else {}
    want = [normalize_url(u) for u in expected_urls]
    result = {'expectedCount': len(want), 'problems': []}
    for field, label in (('desc', 'pc'), ('mobileDesc', 'mobile')):
        got = [normalize_url(u) for u in extract_image_urls(desc.get(field, ''))]
        result[label + 'Count'] = len(got)
        result[label + 'OrderMatches'] = got == want
        if len(got) != len(want):
            result['problems'].append(f'{label} 详情图数量 {len(got)} != 方案 {len(want)}')
        elif got != want:
            result['problems'].append(f'{label} 详情图URL顺序与本地方案不一致')
    return result


def wareeditor_component(urls, existing=None):
    existing = existing if isinstance(existing, dict) else {}

    def entry(html, flag, agent):
        return {'componentCode': 'detail', 'properties': [
            {'key': 'desc', 'value': html},
            {'key': 'zhuangBaUseFlag', 'value': '0' if flag is None else str(flag)},
            {'key': 'userAgent', 'value': agent}]}
    return {'componentCode': 'WareEditor', 'inputType': 'multiText', 'value': [
        entry(detail_html(urls), existing.get('zhuangBaUseFlag'), 'pc'),
        entry(detail_html(urls, mobile=True), existing.get('mobileZhuangBaUseFlag'), 'mobile')]}


def build_detail_patch_body(product_id, category_id, urls, existing=None):
    """PATCH body that changes only WareEditor (PC + mobile detail HTML)."""
    component = wareeditor_component(urls, existing)
    return {'applyId': str(product_id), 'opType': 3, 'scene': 'pop',
            'productInfo': {'productId': int(product_id), 'categoryDetail': {'lastCategoryId': int(category_id)}},
            'productApplyDTO': {'id': str(product_id)},
            'patchOperationList': [{'path': 'product', 'value': json.dumps(component, ensure_ascii=False, separators=(',', ':'))}]}


def delivery_checks(manifest):
    c = dict(manifest.get('deliveryChecks') or {})
    binding = ((manifest.get('video') or {}).get('binding') or {})
    c['videoBound'] = binding.get('stage') == 'VIDEO_VERIFIED'
    for key in ('platformValidationPassed', 'productOnSale', 'detailQualityPassed'):
        c[key] = bool(c.get(key))
    return c


def completion_statement(checks):
    failed = [k for k in ('platformValidationPassed', 'productOnSale', 'detailQualityPassed') if not checks.get(k)]
    if failed:
        return '上架未完成：' + '、'.join(failed) + ' 未通过'
    return '全部完成' if checks.get('videoBound') else '上架完成（主图视频尚未绑定，不能称全部完成）'


def source_detail_problems(product_detail_desc, exemption_reason=''):
    """Matrix/clone routes keep the source product's own detail HTML: it must be complete on PC and mobile."""
    desc = product_detail_desc if isinstance(product_detail_desc, dict) else {}
    pc = [normalize_url(u) for u in extract_image_urls(desc.get('desc', ''))]
    mobile = [normalize_url(u) for u in extract_image_urls(desc.get('mobileDesc', ''))]
    if str(exemption_reason or '').strip():
        return []
    problems = []
    if len(pc) < MIN_MODULES:
        problems.append(f'源商品详情页只有{len(pc)}张图，低于{MIN_MODULES}张：复制出的新商品也会不完整；请先用 detail-repair 补齐，或由用户明确只要少量并记录原因')
    if mobile and mobile != pc:
        problems.append('源商品 PC 与手机端详情图数量/顺序不一致，先修复源详情页')
    return problems
