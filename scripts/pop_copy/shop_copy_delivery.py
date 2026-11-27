"""Compare the shipping and return terms of two verified shops' own products."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from .safety import redact
from . import CloudFileUnavailable


FREIGHT_API = 'GET.sp-seller.v0.shipping-templates._templateId'
FREIGHT_HELP = '在该店商品编辑页核对已选模板 ID，再从该店运费模板页读取计费方式、运费承担方、配送区域、首续数量及金额和例外条件。'
PROMISE_HELP = '在该店商品和 SKU 编辑页分别核对发货时效的选中值、是否继承及实际承诺，查看时效模板的截止时间和例外条件。'
MISSING = object()

# Only explicit schema names or exact field definitions, never guessed ordinal codes.
_FREIGHT_FIELDS = {
    'templateId':('运费模板ID','运费模版ID','模板ID'), 'templateName':('运费模板名称','模板名称'),
    'shopId':('店铺ID',), 'venderId':('商家ID',), 'rules':('模板规则',),
    'payer':('运费承担方','运费支付方'), 'chargeBy':('计费方式','计价方式'), 'currency':('币种',),
    'unit':('计费单位',), 'zones':('区域计费规则','运费区域规则'), 'regions':('配送区域','地区编码列表'),
    'firstQuantity':('首件数量','首重','首体积'), 'firstFee':('首费','首件费用','首重费用'),
    'nextQuantity':('续件数量','续重','续体积'), 'nextFee':('续费','续件费用','续重费用'),
}


def _described_fields(record, schema):
    if not isinstance(record,dict) or not isinstance(schema,dict) or schema.get('type')!='object':
        raise ValueError('模板回包结构未由能力卡确认')
    props = schema.get('properties') or {}
    if not props or set(record)!=set(props):
        raise ValueError('回包与字段定义未完整对应')
    values, definitions = {}, {}
    for name,value in record.items():
        spec = props[name]
        if not isinstance(spec,dict):
            raise ValueError('缺少字段定义')
        label = spec.get('title') or spec.get('description')
        key = next((k for k,labels in _FREIGHT_FIELDS.items() if name==k or label in labels),name)
        if key in values:
            raise ValueError('字段定义不唯一')
        values[key],definitions[key] = value,spec
    return values,definitions


def _enum_text(value, spec, names):
    # For a numeric platform enum, use only a oneOf const/title pair printed by the card.
    for item in spec.get('oneOf') or []:
        if isinstance(item,dict) and 'const' in item and item['const']==value:
            value = item.get('title')
            break
    return names.get(value,value) if isinstance(value,str) else value


def _official_template(card, data, tid, shop, digest):
    """Project a fully described single-template read. Incomplete/unlabelled shapes stay raw."""
    schema = card.get('outputSchema')
    try:
        if isinstance(schema,str):
            schema = json.loads(schema)
        if isinstance(schema,dict) and 'data' in (schema.get('properties') or {}) and 'data' not in data:
            schema = schema['properties']['data']
        row,defs = _described_fields(data,schema)
        if _id(row.pop('templateId',None)) != tid or redact(data)!=data:
            return None
        for field in ('shopId','venderId'):
            if field in row and str(row.pop(field))!=str(shop[field]):
                return None
        name = row.pop('templateName',None)
        if set(row)=={'rules'}:
            row,defs = _described_fields(row['rules'],defs['rules'])
        if not {'payer','chargeBy','unit','currency','zones'}<=set(row) or not isinstance(row['zones'],list):
            return None
        if any(key.lower() in ('page','pageno','pageindex','pagesize','total','hasmore','nextcursor','nextpage') for key in row):
            return None
        row['payer'] = _enum_text(row['payer'],defs['payer'],{'买家承担':'buyer','卖家承担':'seller','按条件':'conditional'})
        row['chargeBy'] = _enum_text(row['chargeBy'],defs['chargeBy'],{'按件':'item','按重量':'weight','按体积':'volume'})
        if row['chargeBy'] not in ('item','weight','volume') or not row['unit'] or not row['currency']:
            return None
        zones = []
        for raw in row['zones']:
            zone,_ = _described_fields(raw,defs['zones'].get('items'))
            zones.append(zone)
        row['zones'] = zones
        template = {'id':tid,'name':name,'rules':row,'complete':True,'readSha256':digest,
                    'receipt':FREIGHT_API+'/'+tid+'#'+digest}
        _rules(template,'freightTemplates')
        return template
    except (AttributeError,KeyError,TypeError,ValueError):
        return None


def _id(value):
    text = str(value)
    return text if re.fullmatch('[1-9][0-9]*',text) else None


def _same_shop(given, expected):
    return (isinstance(given,dict) and isinstance(expected,dict) and expected.get('profile') not in (None,'','default')
            and _id(expected.get('shopId')) and _id(expected.get('venderId'))
            and all(str(given.get(k,'')) == str(expected[k]) for k in ('profile','shopId','venderId')))


def _schema_options(schema):
    fields = {'WareTransport':set(),'WarePromise':set(),'SkuPromise':set(),'WareSevenDayReturn':set()}
    def visit(node):
        if isinstance(node,list):
            for child in node:
                visit(child)
        elif isinstance(node,dict):
            code = node.get('componentCode')
            if code in fields:
                for option in node.get('optionsValueList') or node.get('options') or []:
                    value = option.get('value') if isinstance(option,dict) else None
                    if value is not None and str(value).strip():
                        fields[code].add(str(value))
            for key in ('schema','components','childrenComponents','children','data'):
                if key in node:
                    visit(node[key])
        elif isinstance(node,str):
            try:
                parsed = json.loads(node)
            except ValueError:
                return
            if isinstance(parsed,(list,dict)):
                visit(parsed)
    visit(schema)
    return {code:sorted(values) for code,values in fields.items()}


def _freight_route(card):
    if not isinstance(card,dict) or card.get('apiName') != FREIGHT_API or card.get('method') != 'GET':
        raise ValueError('capability-card-mismatch')
    path = str(card.get('path') or '')
    if path.startswith('/sp-seller/'):
        path = '/rest'+path
    if not re.fullmatch(r'/rest/sp-seller/v[0-9]+/[A-Za-z0-9_/-]+/\{templateId\}',path):
        raise ValueError('capability-path-unavailable')
    schema = card.get('inputSchema')
    if isinstance(schema,str):
        schema = json.loads(schema)
    if not isinstance(schema,dict) or set(schema.get('required') or [])-{'templateId'}:
        raise ValueError('capability-input-needs-review')
    parameter = (schema.get('properties') or {}).get('templateId') or {}
    if parameter.get('type') not in ('integer','string'):
        raise ValueError('capability-input-needs-review')
    return path, parameter['type']


def capture_freight(api, detail, schema, shop, template_ids=None):
    """Read only IDs observed in this shop. Inputs and output use the live capability card."""
    if not _same_shop(api.require_verified_jdo_store(),shop):
        raise ValueError('运费采证的活动 Profile 与本次已核店身份不一致')
    options = _schema_options(schema)
    if template_ids is not None and (not isinstance(template_ids,(list,tuple,set)) or any(not _id(t) for t in template_ids)):
        raise ValueError('候选模板 ID 必须来自该店，并逐项提供')
    info = (detail or {}).get('productInfo') or {}
    selected = (info.get('logisticsInfo') or {}).get('transportId')
    ids = {_id(value) for value in [selected,*(template_ids or [])]} - {None}
    if not _id(selected):
        ids.update(value for value in options['WareTransport'] if _id(value))
    out = {'shop':{k:shop[k] for k in ('shopId','venderId','profile')}, 'schemaOptionIds':options,
           'checkedAt':datetime.now(timezone.utc).isoformat(timespec='seconds'), 'freightReads':{}}
    if not ids:
        out.update(status='needs-template-id',nextAction=FREIGHT_HELP)
        return out
    try:
        profile = str(shop['profile'])
        card = api.run_jdo(['info','--profile',profile,'--skill-id','jdo-api-830','-a',FREIGHT_API])
        route,kind = _freight_route(card)
    except CloudFileUnavailable:
        raise
    except (ValueError,OSError,RuntimeError,TypeError):
        out.update(status='needs-official-page',nextAction=FREIGHT_HELP)
        return out
    out['capability'] = {'apiName':FREIGHT_API,'method':'GET','path':route,
                         'inputSchema':redact(card['inputSchema']),'outputSchema':redact(card.get('outputSchema'))}
    for tid in sorted(ids):
        try:
            result = api.run_jdo(['invoke','--profile',profile,'--skill-id','jdo-seller-info','--method','GET',
                                  '--url',route,'--params',json.dumps({'templateId':int(tid) if kind=='integer' else tid}),
                                  '--request-identity','vender'])
            data = result.get('data') if isinstance(result,dict) and result.get('success') is not False else None
            if not isinstance(data,(dict,list)) or not data:
                raise ValueError('missing-template-details')
            raw = json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
            digest = hashlib.sha256(raw).hexdigest()
            out['freightReads'][tid] = {'status':'read', 'apiName':FREIGHT_API, 'templateId':tid,
                'requestId':str(result.get('requestId') or ''),'sha256':digest,'data':redact(data)}
            template = _official_template(card,data,tid,shop,digest)
            if template:
                out.setdefault('freightTemplates',[]).append(template)
        except CloudFileUnavailable:
            raise
        except (ValueError,OSError,RuntimeError,TypeError):
            out['freightReads'][tid] = {'status':'needs-official-page','templateId':tid}
    out['status'] = 'read' if all(r['status']=='read' for r in out['freightReads'].values()) else 'needs-official-page'
    out['nextAction'] = '按本次返回字段解释模板完整规则，并保留原请求引用。未能读全时，'+FREIGHT_HELP
    return out


def _number(value, positive=False):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation,ValueError):
        raise ValueError('缺少有效的数量或金额') from None
    if not amount.is_finite() or amount < 0 or (positive and not amount):
        raise ValueError('模板数量或金额不完整')
    if not amount:
        return '0'
    text = format(amount,'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _rules(template, kind):
    if not template.get('receipt') or template.get('complete') is not True:
        raise ValueError('模板详情缺少完整规则或来源回执')
    rules = deepcopy(template.get('rules'))
    if not isinstance(rules,dict):
        raise ValueError('缺少已核对的模板详情')
    if kind == 'promiseTemplates':
        promise = rules.get('commitment')
        if isinstance(promise,dict):
            if promise.get('unit') not in ('minutes','hours','days','business-days') or not promise.get('startsAt'):
                raise ValueError('缺少发货计时起点、期限或单位')
            promise['duration'] = _number(promise.get('duration'))
        elif not isinstance(promise,str) or not re.search(
                r'(?:\d+(?:\.\d+)?|[一二两三四五六七八九十]+)\s*个?(?:小时|分钟|工作日|天|日|周|月)'
                r'|当天|当日|次日|隔日|\d+\s*(?:minute|hour|day|week)s?\b',promise,re.I):
            raise ValueError('缺少实际发货承诺')
        return rules
    if rules.get('payer') not in ('buyer','seller','conditional'):
        raise ValueError('缺少运费承担方式')
    if rules['payer']=='conditional' and not rules.get('conditions'):
        raise ValueError('缺少条件运费适用条件')
    zones = rules.get('zones')
    if not isinstance(zones,list) or not zones:
        raise ValueError('缺少区域和费用规则')
    seen = set()
    disjoint = True
    for zone in zones:
        regions = zone.get('regions') if isinstance(zone,dict) else None
        if not isinstance(regions,list) or not regions or any(not isinstance(r,str) or not r.strip() for r in regions):
            raise ValueError('区域须来自模板实际覆盖范围')
        zone['regions'] = sorted(set(regions))
        disjoint = disjoint and not seen.intersection(regions)
        seen.update(regions)
        if rules['payer']=='seller' or zone.get('type') in ('free','excluded'):
            continue
        if (rules.get('chargeBy') not in ('item','weight','volume') or
                not rules.get('unit') or not rules.get('currency')):
            raise ValueError('缺少计费方式、基本单位或币种')
        for field in ('firstQuantity','firstFee','nextQuantity','nextFee'):
            zone[field] = _number(zone.get(field),positive=field.endswith('Quantity'))
    if disjoint:
        zones.sort(key=lambda z:z['regions'])
    # Unknown captured conditions also participate in equality. No default pricing or regions.
    return rules


def _templates(side,kind):
    rows = side.get(kind,[])
    if not isinstance(rows,list) or any(not isinstance(row,dict) or not _id(row.get('id')) for row in rows):
        raise ValueError('模板表需要该店真实模板 ID')
    by_id = {str(row['id']):row for row in rows}
    if len(by_id)!=len(rows):
        raise ValueError('同店模板 ID 有重复证据，请核对最新回执')
    for tid,row in by_id.items():
        if (any(k in row and str(row[k])!=str(side['shop'][k]) for k in ('shopId','venderId','profile'))
                or ('shop' in row and not _same_shop(row['shop'],side['shop']))):
            raise ValueError('模板明细属于其他店铺或 Profile')
        if 'readSha256' in row:
            original = (side.get('freightReads') or {}).get(tid) or {}
            if kind!='freightTemplates' or original.get('status')!='read' or original.get('sha256')!=row['readSha256']:
                raise ValueError('模板规则引用的原始回包哈希不匹配')
    return by_id


def _selection(detail, side, code, sku=None):
    info = (detail or {}).get('productInfo') or {}
    if sku is not None:
        rows = [r for r in (detail or {}).get('skuList') or [] if str(r.get('skuId')) == sku]
        if len(rows)!=1:
            return MISSING
        found = [(rows[0].get(k) or {})['promiseId'] for k in ('logistics','logisticsInfo') if 'promiseId' in (rows[0].get(k) or {})]
    elif code in ('WareTransport','WarePromise'):
        name = 'transportId' if code=='WareTransport' else 'promiseId'
        logistics = info.get('logisticsInfo') or {}
        found = [logistics[name]] if name in logistics else []
    else:
        key = 'is7ToReturn' if code=='WareSevenDayReturn' else 'isPayFirst'
        found = [r.get('value') for r in info.get('features') or [] if r.get('key')==key]
    # A scoped visible-page observation can supply an omitted field, e.g. an explicitly inherited SKU.
    observed = side.get('productSelections') or {}
    if observed.get('receipt') and _id(observed.get('productId')) == _id(info.get('productId')) and _id(info.get('productId')):
        selected = observed.get('SkuPromise',{}) if sku is not None else observed
        key = sku if sku is not None else code
        if key in selected:
            found.append(selected[key])
    if any(v is not None and (type(v) not in (str,int) or (isinstance(v,str) and v.strip()!=v)) for v in found):
        return MISSING
    values = {str(v) if v not in (None,'') else None for v in found}
    return values.pop() if len(values)==1 else MISSING


def compare_delivery(source_detail, target_detail, source_shop, target_shop, sku_pairs, evidence=None):
    """Prepare equivalences or verify the actual selected target SPU/SKU fields."""
    evidence = evidence or {}
    if not _same_shop(source_shop,source_shop) or not _same_shop(target_shop,target_shop):
        raise ValueError('配送核对需要源店与目标店的各自已验证身份')
    sides = []
    for name,shop in (('source',source_shop),('target',target_shop)):
        side = evidence.get(name,{})
        if side and not _same_shop(side.get('shop'),shop):
            raise ValueError('配送证据的店铺或 Profile 与该侧商品不一致')
        sides.append(side)
    source,target = sides
    checks,values = [],{}
    preparing = target_detail is None

    def check(code, sku=None):
        field = f'SkuPromise[{sku}]' if sku is not None else code
        selected = _selection(source_detail,source,code,sku)
        dest = str((sku_pairs or {}).get(sku,'')) if sku is not None else None
        current = _selection(target_detail,target,code,dest) if not preparing else MISSING
        row = {'field':field, 'sourceValue':None if selected is MISSING else selected,
               'targetValue':None if current is MISSING else current}
        checks.append(row)
        if selected is MISSING:
            row.update(status='needs-evidence',nextAction='核对源商品此项的实际选中值。'+
                       (FREIGHT_HELP if code=='WareTransport' else PROMISE_HELP if 'Promise' in code else '保留源商品编辑页回执。'))
            return
        if selected is None:
            if code == 'WareTransport':
                try:
                    source_rules = _rules(source.get('defaultFreight') or {},'freightTemplates')
                    target_rules = _rules(target.get('defaultFreight') or {},'freightTemplates')
                except ValueError:
                    row.update(status='needs-evidence',nextAction='运费选择为空，请分别核对两店商品实际生效的默认运费。'+FREIGHT_HELP)
                    return
                row.update(sourceReceipt=source['defaultFreight']['receipt'],targetReceipt=target['defaultFreight']['receipt'])
                if source_rules!=target_rules:
                    row.update(status='different',nextAction='两店默认运费规则有差异，请先在目标店选择并回读等价模板。')
                    return
            elif code not in ('SkuPromise','WarePromise'):
                row.update(status='needs-evidence',nextAction='该字段为空，请核对源商品实际适用的配置。')
                return
            # An explicit empty promise records the absence of an override at this level.
            row.update(status='ready-to-apply' if preparing else 'needs-evidence' if current is MISSING else
                       'verified' if current is None else 'different', expectedTargetValue=None)
            if row['status']=='needs-evidence':row['nextAction']=PROMISE_HELP
            return
        expected = selected
        return_options = target.get('schemaOptionIds',{}).get('WareSevenDayReturn') or []
        if code=='WareSevenDayReturn' and return_options and selected not in return_options and current!=selected:
            row.update(status='needs-evidence',nextAction='源店退货代码未出现在目标类目选项，请核对该店允许的退货承诺。')
            return
        if code in ('WareTransport','WarePromise','SkuPromise'):
            kind = 'freightTemplates' if code=='WareTransport' else 'promiseTemplates'
            advice = FREIGHT_HELP if code=='WareTransport' else PROMISE_HELP
            try:
                src = _templates(source,kind).get(selected) or {}
                terms = _rules(src,kind)
                targets = _templates(target,kind)
            except ValueError as exc:
                row.update(status='needs-evidence',nextAction=f'{exc}。{advice}')
                return
            matches = []
            for tid,template in targets.items():
                if not (template.get('selectable') is True or current==tid or tid in target.get('schemaOptionIds',{}).get(code,[])):
                    continue
                try:
                    if _rules(template,kind)==terms:
                        matches.append(tid)
                except ValueError:
                    continue
            if not matches:
                row.update(status='needs-evidence',sourceReceipt=src['receipt'],
                           nextAction='查目标店可选模板并核对完整规则。确认无等价模板后，按授权在目标官方页面创建并回读新 ID。'+advice)
                if current in targets:
                    try:
                        if _rules(targets[current],kind)!=terms:
                            row.update(status='different',targetReceipt=targets[current]['receipt'])
                    except ValueError:
                        pass
                return
            expected = current if current in matches else sorted(matches,key=int)[0]
            row.update(sourceReceipt=src['receipt'],targetReceipt=targets[expected]['receipt'],
                       equivalentTargetIds=sorted(matches,key=int))
        if sku is None:
            values[code] = expected
        else:
            values.setdefault('SkuPromise',{})[sku] = expected
        row['expectedTargetValue'] = expected
        row['status'] = 'ready-to-apply' if preparing else ('needs-evidence' if current is MISSING else
                        'verified' if current==expected else 'different')
        if row['status']=='needs-evidence':
            row['nextAction']='从目标商品编辑详情或官方页面回读同一字段。'

    for code in ('WareTransport','WarePromise','WareSevenDayReturn','isPayFirst'):
        check(code)
    skus = [str(item.get('skuId') or '') for item in source_detail.get('skuList') or []]
    if not skus or any(not _id(sku) for sku in skus) or len(skus)!=len(set(skus)):
        checks.append({'field':'SkuPromise','status':'needs-evidence','nextAction':'请核对源商品完整 SKU 与其发货时效。'})
    else:
        for sku in skus:
            check('SkuPromise',sku)
    if (_selection(source_detail,source,'WarePromise') is None and
            any(_selection(source_detail,source,'SkuPromise',sku) is None for sku in skus)):
        row = {'field':'defaultPromise'}
        try:
            left = _rules(source.get('defaultPromise') or {},'promiseTemplates')
            right = _rules(target.get('defaultPromise') or {},'promiseTemplates')
            row.update(status=('ready-to-apply' if preparing else 'verified') if left==right else 'different',
                       sourceReceipt=source['defaultPromise']['receipt'],targetReceipt=target['defaultPromise']['receipt'])
        except ValueError:
            row.update(status='needs-evidence',nextAction='未设置独立时效的 SKU 需查两店实际继承的承诺。'+PROMISE_HELP)
        checks.append(row)
    status = ('different' if any(r['status']=='different' for r in checks) else
              'needs-evidence' if any(r['status']=='needs-evidence' for r in checks) else
              'ready-to-apply' if preparing else 'verified')
    return {'status':status,'targetValues':values,'checks':checks,
            'needsEvidence':[r['field'] for r in checks if r['status']=='needs-evidence']}
