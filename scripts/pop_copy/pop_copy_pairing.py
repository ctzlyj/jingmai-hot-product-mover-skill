"""Pair live product specifications and validate source identity for POP migration."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re

PUBLIC_GROUP = '0000000000'

def sku_rows(detail, pid):
    if str(detail.get('productInfo', {}).get('productId')) != str(pid):
        raise ValueError('官方商品身份不一致')
    rows = {}
    for row in detail.get('skuList') or []:
        sid = str(row.get('skuId') or '')
        if not sid.isdecimal() or sid in rows or str(row.get('productId', pid)) != str(pid):
            raise ValueError('商品规格身份重复、缺失或跨商品')
        rows[sid] = row
    if not rows:
        raise ValueError('未读到完整规格')
    return rows


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode()).hexdigest()


def _shop(identity):
    normalized = {k: str(identity.get(k) or '') for k in ('profile','shopName','shopId','venderId')}
    if (not normalized['shopName'].strip() or normalized['profile'].lower() in ('', 'default')
            or not all(re.fullmatch(r'[1-9][0-9]*', normalized[k]) for k in ('shopId','venderId'))):
        raise ValueError('复制商品需要两家店已核验的 Profile、店名、shopId 和 venderId')
    return normalized


def _product_id(identity, detail):
    product = detail.get('productInfo') or {}
    pid = str(product.get('productId') or '')
    if not re.fullmatch(r'[1-9][0-9]*', pid):
        raise ValueError('官方商品详情缺少明确 productId')
    for container in (detail,product):
        for field in ('shopId','venderId'):
            if field in container and str(container[field]) != identity[field]:
                raise ValueError('商品回读店铺身份与当前 Profile 不符')
    return pid


def freeze_copy(source_shop, target_shop, source_detail, *, stock_rows=None, video_rows=None, source_components=None):
    source, target = _shop(source_shop), _shop(target_shop)
    if source['shopId']==target['shopId'] or source['profile'].casefold()==target['profile'].casefold():
        raise ValueError('跨店复制需要两个不同的已核验店铺及各自 Profile')
    pid = _product_id(source,source_detail)
    sku_rows(source_detail,pid)
    if video_rows is not None:
        video_rows={str(sid):rows for sid,rows in video_rows.items()}
    value = {'mode':'owned-shop-copy','sourceShop':source,'targetShop':target,'sourceProductId':pid,
             'sourceDetail':deepcopy(source_detail),'sourceStocks':deepcopy(stock_rows),'sourceVideos':deepcopy(video_rows)}
    if source_components:
        nodes = list(_template_nodes(source_components))
        value['sourceComponentCodes'] = sorted({n['componentCode'] for n in nodes})
        value['sourceDetailFields'] = sorted({n['productDetailField'] for n in nodes if isinstance(n.get('productDetailField'),str)})
    return {**value,'sha256':_digest(value)}


def _validate(frozen, target=None):
    if frozen.get('mode')!='owned-shop-copy' or _digest({k:v for k,v in frozen.items() if k!='sha256'})!=frozen.get('sha256'):
        raise ValueError('原商品复制快照缺失或已改变')
    if target is not None and _shop(target)!=frozen['targetShop']:
        raise ValueError('目标店铺或 Profile 与这份复制快照不一致')


def _number(value, integer=False):
    try:
        number=Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    if not number.is_finite() or number<0 or (integer and number!=number.to_integral_value()):
        return None
    if integer:
        return int(number)
    return format(number.normalize(),'f')


def _specs(row):
    if not isinstance(row.get('saleAttrs'),list):
        return None
    dimensions={}
    for attr in row['saleAttrs']:
        dim=_number(attr.get('indexId') or attr.get('dim') or (1 if len(row['saleAttrs'])==1 else None),True)
        aid=str(attr.get('attrId') or '')
        name=attr.get('attrName')
        alias=attr.get('attrValueAlias')
        if not dim or dim in dimensions or not aid or not name or not isinstance(alias,str) or not alias.strip():
            return None
        spec={'dimension':dim,'attrId':aid,'name':name,'value':alias}
        if attr.get('templateSkuProps'):
            boxes=attr['templateSkuProps']
            if isinstance(boxes,str):
                try:boxes=json.loads(boxes)
                except ValueError:return None
            if not isinstance(boxes,list):return None
            spec['templateSkuProps']=boxes
        dimensions[dim]=spec
    return [dimensions[i] for i in sorted(dimensions)]


def _groups(material, name):
    if not isinstance(material.get(name),list):
        return None
    groups={}
    for group in material[name]:
        gid=str(group.get('uuid') or '')
        if not gid or gid in groups or not isinstance(group.get('imageInfoList'),list):
            return None
        slots={}
        for item in group['imageInfoList']:
            pos=_number(item.get('orderSort'),True)
            url=item.get('imgUrl')
            if not pos or pos in slots or not isinstance(url,str) or not url:
                return None
            slots[pos]=url
        if sorted(slots)!=list(range(1,len(slots)+1)):
            return None
        groups[gid]=[slots[i] for i in sorted(slots)]
    return groups


def _gallery_key(row,sid,groups):
    if groups is None:
        return None
    if set(groups)=={PUBLIC_GROUP}:
        return PUBLIC_GROUP
    attrs=row.get('saleAttrs') or []
    keys={sid}
    for attr in attrs:
        dim=attr.get('indexId') or attr.get('dim')
        if str(dim)=='1' or (dim in (None,'') and len(attrs)==1):
            keys.add(str(attr.get('attrValues') or ''))
    matches=(set(groups)&keys)-{PUBLIC_GROUP}
    return matches.pop() if len(matches)==1 else None


def _view(detail,stock_rows,video_rows):
    product=detail['productInfo']
    rows=sku_rows(detail,product['productId'])
    material=detail.get('material') or {}
    groups=_groups(material,'mainImages')
    rectangle=_groups(material,'rectangleImages')
    stocks={}
    for row in stock_rows or []:
        sid=str(row.get('skuId') or '')
        if sid not in rows or sid in stocks or str(row.get('productId',product['productId']))!=str(product['productId']):
            raise ValueError('库存回执包含其他商品或重复 SKU')
        stocks[sid]=_number(row.get('stockNum'),True)
    videos={} if video_rows is None else {str(k):v for k,v in video_rows.items()}
    if set(videos)-set(rows):
        raise ValueError('视频回执包含其他商品的 SKU')
    spec_images=None
    if isinstance(material.get('ggtImages'),list):
        owners={str(row.get('uuid') or k):k for k,row in rows.items()}
        spec_images={sid:[] for sid in rows}
        for image in material['ggtImages']:
            sid=owners.get(str(image.get('uuid') or ''))
            if len(owners)!=len(rows) or sid is None or spec_images[sid] or not image.get('imageUrl'):
                spec_images=None
                break
            spec_images[sid]=[image['imageUrl']]
    skus={}
    for sid,row in rows.items():
        vm=videos.get(sid)
        video_ids=None
        if isinstance(vm,list):
            ids=[str((v.get('videoMaterial') or {}).get('videoId') or '') for v in vm]
            if all(re.fullmatch(r'[1-9][0-9]*',v) for v in ids):
                video_ids=sorted(ids)
        skus[sid]={
            'merchantSku':None if row.get('outerId') is None else str(row['outerId']),
            'specifications':_specs(row),'price':_number((row.get('priceInfo') or {}).get('jdPrice')),
            'stock':stocks.get(sid) if stock_rows is not None else _number(row.get('stockNum'),True),
            'mainGroup':_gallery_key(row,sid,groups),'rectangleGroup':_gallery_key(row,sid,rectangle),
            'specImages':spec_images.get(sid) if spec_images is not None else None,
            'logo':None if row.get('logo') is None else [row['logo']] if row['logo'] else [],
            'videoIds':video_ids,'attributes':row.get('goodsAttrInfos'),
            'upcCode':row.get('upcCode'),'enabled':_number(row.get('skuEnableStatus'),True)}
    width=product.get('wide') if 'wide' in product else product.get('width')
    if 'wide' in product and 'width' in product and _number(product['wide'])!=_number(product['width']):
        width=None
    brand=(product.get('brandInfo') or {}).get('brandId',product.get('brandId'))
    desc=product.get('productDetailDesc')
    desc=desc if isinstance(desc,dict) else {}
    return {'productId':str(product['productId']),'title':product.get('productName'),'itemNumber':product.get('itemNum'),
            'categoryId':str((product.get('categoryDetail') or {}).get('lastCategoryId') or '') or None,
            'brandId':str(brand) if brand is not None else None,'attributes':product.get('goodsAttrInfos'),
            'packaging':{k:_number(v) for k,v in (('length',product.get('length')),('width',width),
                                                   ('height',product.get('height')),('weight',product.get('weight')))},
            'packSpecification':product.get('packSpecification'),'mainGroups':groups,'rectangleGroups':rectangle,
            'details':{key:desc.get(key) for key in ('desc','mobileDesc')},
            'skus':skus}


def _spec_signature(specs):
    if specs is None:
        return None
    # An unproven enum-ID change is not a writable SKU association. A caller
    # with the target's verified template may first project the source options.
    result=deepcopy(specs)
    for axis in result:
        for box in axis.get('templateSkuProps') or []:
            if not isinstance(box,dict) or box.get('id') in (None,'') or box.get('value') in (None,''):
                return None
            box['id']=str(box['id'])
            if box.get('valueId') is not None:
                box['valueId']=str(box['valueId'])
    return _digest(result)


def pair_copy_skus(source,target):
    """Return independent, one-to-one evidence. Never use the code being checked as the join key."""
    sources={sid:_spec_signature(s['specifications']) for sid,s in source.items()}
    counts=Counter(sources.values())
    index={}
    for sid,s in target.items():
        key=_spec_signature(s['specifications'])
        if key is not None:index.setdefault(key,[]).append(sid)
    pairs={}; reasons={}
    for sid,key in sources.items():
        if key is None:reasons[sid]='source-specifications-incomplete'
        elif counts[key]!=1:reasons[sid]='source-specifications-not-unique'
        elif key not in index:reasons[sid]='no-exact-target-specifications'
        elif len(index[key])!=1:reasons[sid]='target-specifications-not-unique'
        elif not source[sid]['specifications'] and (len(source)!=1 or len(target)!=1):
            reasons[sid]='empty-specifications-in-multiple-sku-product'
        else:pairs[sid]=index[key][0]
    return {'pairs':pairs,'reasons':reasons,'matchedBy':{sid:'unique-complete-specifications' for sid in pairs}}


def _copy_pairing(frozen,source_skus,target_skus,target_schema):
    pair_source=source_skus
    spec_gaps={}
    if target_schema is not None:
        _validate_reference(frozen,target_schema)
        pair_source=deepcopy(pair_source)
        catalog={n['componentCode']:n for n in _template_nodes((target_schema.get('schema') or {}).get('componentList') or [])}
        for sid,raw in sku_rows(frozen['sourceDetail'],frozen['sourceProductId']).items():
            axes={str(a.get('attrId')):a for a in raw.get('saleAttrs') or []}
            for axis in pair_source[sid]['specifications'] or []:
                key=axis['attrId']
                try:
                    props=_target_props(axes[key],catalog.get('sale-prop-'+key,{}),target_schema)
                except ValueError as error:
                    spec_gaps.setdefault(sid,[]).append(str(error))
                    continue
                if props is not None:
                    axis['templateSkuProps']=props
    pairing=pair_copy_skus(pair_source,target_skus)
    if pairing['reasons']:
        _resolve_display_aliases(pairing,pair_source,target_skus,spec_gaps,target_schema)
    return pair_source,pairing,spec_gaps


def _resolve_display_aliases(pairing,source,target,gaps,reference):
    """A display alias can be truncated. Only a complete, independently verified option tuple can authorize a join."""
    templates=(reference or {}).get('structuredSaleTemplates') or {}
    def key(row):
        specs=deepcopy(row['specifications'])
        if not specs or not any(str(a['attrId']) in templates and a.get('templateSkuProps') for a in specs):
            return None
        for axis in specs:
            if str(axis['attrId']) in templates and axis.get('templateSkuProps'):
                axis.pop('value',None)  # retain every structured value, option ID, unit and other axis
        return _spec_signature(specs)
    left={sid:key(row) for sid,row in source.items()}
    sizes=Counter(left.values())
    right={}
    for tid,row in target.items():
        right.setdefault(key(row),[]).append(tid)
    used=set(pairing['pairs'].values())
    candidates={}
    for sid in list(pairing['reasons']):
        options=right.get(left[sid],[])
        if (sid not in gaps and left[sid] is not None and sizes[left[sid]]==1 and len(options)==1
                and options[0] not in used):
            tid=options[0]
            pairing['pairs'][sid]=tid
            used.add(tid)
            pairing['matchedBy'][sid]='unique-target-template-values'
            del pairing['reasons'][sid]
            continue
        a=source[sid]['specifications']
        if not a:continue
        possible=[]
        for tid,row in target.items():
            b=row['specifications']
            if tid in used or not b or len(a)!=len(b):continue
            if all((x['dimension'],x['attrId'])==(y['dimension'],y['attrId']) and
                   (x['value'].startswith(y['value']) or y['value'].startswith(x['value'])) for x,y in zip(a,b)):
                possible.append(tid)
        if possible:candidates[sid]=possible
    pairing['displayOnlyCandidates']=candidates


def pair_frozen_target_skus(frozen,target_detail,target_schema=None):
    """Independently pair SKUs for an authorized edit; unresolved template projections cannot write."""
    _validate(frozen)
    target_pid=_product_id(frozen['targetShop'],target_detail)
    if target_pid==frozen['sourceProductId']:
        raise ValueError('目标回读仍指向原 SPU')
    left=_view(frozen['sourceDetail'],None,None)['skus']
    right=_view(target_detail,None,None)['skus']
    _,pairing,gaps=_copy_pairing(frozen,left,right,target_schema)
    for sid in gaps:
        pairing['pairs'].pop(sid,None)
        pairing['matchedBy'].pop(sid,None)
        pairing['reasons'][sid]='target-template-projection-unverified'
    return {**pairing,'specGaps':gaps}


def _template_nodes(nodes):
    for node in nodes:
        if not isinstance(node,dict):continue
        if node.get('componentCode'):
            yield node
        yield from _template_nodes(node.get('childrenComponents') or [])


def _structured(node):
    return str((node.get('extra') or {}).get('isStructure','')).lower() in ('true','1')


def _target_props(attr, node, reference):
    """Align independently observed source box IDs to a verified target template.

    Target-local option IDs can change. The displayed alias, units and each
    individual source value must survive the transfer.
    """
    from .sale_attr_templates import build_props

    aid=str(attr['attrId'])
    templates=reference.get('structuredSaleTemplates') or {}
    template=templates.get(aid)
    source=attr.get('templateSkuProps')
    if isinstance(source,str) and source:
        try:source=json.loads(source)
        except ValueError:raise ValueError('原结构化分项 JSON 无法解析') from None
    if not (source or _structured(node) or template):
        return None
    if not isinstance(template,dict) or str(template.get('attrId'))!=aid:
        raise ValueError('需从目标店当前末级类目的 sale-attr-templates 或同类目在售商品核实填写框、单位与选项')
    proof=template.get('source') or {}
    category=str(reference.get('categoryId') or (reference.get('category') or {}).get('id'))
    if proof.get('kind')=='same-shop-same-category-product':
        if str(proof.get('categoryId'))!=category or not proof.get('productId') or not proof.get('samples'):
            raise ValueError('目标参考商品缺少同类目和当前店铺内的样本证据')
    elif proof.get('kind')!='official-interface':
        raise ValueError('缺少当前目标店铺结构化模板的来源回执')
    if (('shopIdentity' in proof and _shop(proof['shopIdentity'])!=_shop(reference['shopIdentity']))
            or ('categoryId' in proof and str(proof['categoryId'])!=category)):
        raise ValueError('模板回执的店铺或类目与目标不符')
    boxes=template.get('inputs') or []
    keys=[str(b['id']) if b.get('id') is not None else '' for b in boxes]
    if not boxes or '' in keys or len(set(keys))!=len(keys):
        raise ValueError('目标模板填写框缺失或重复')
    alias=attr.get('attrValueAlias')
    if not source:
        # A complete single enumerated value can be mapped without splitting a
        # legacy alias into guesses, even when the source exposed no input boxes.
        box=boxes[0]
        if len(boxes)==1 and any(str(o['value'])+str(box.get('unit') or '')==alias for o in box.get('options') or []):
            value=next(o['value'] for o in box['options'] if str(o['value'])+str(box.get('unit') or '')==alias)
            source=[{'id':box['id'],'value':value,'unit':box.get('unit') or ''}]
        else:
            raise ValueError('源商品没有结构化分项，请从原通过体或源编辑回执取原始分项，不按标签切词')
    if not isinstance(source,list):
        raise ValueError('原结构化分项不是可核验的字段数组')
    originals={}
    for original in source:
        if not isinstance(original,dict):
            raise ValueError('原结构化分项不能定位')
        key=str(original['id']) if original.get('id') is not None else ''
        if (key not in keys or key in originals or original.get('value') in (None,'')
                or any(k not in ('id','value','valueId','unit') and v not in (None,'') for k,v in original.items())):
            raise ValueError('存在缺失、重复或不能映射至目标模板的原分项')
        originals[key]=original
    inputs=[]; values=[]
    for key,box in zip(keys,boxes):
        item=originals.get(key)
        if item is None:
            if box.get('optional') is True:continue
            raise ValueError('缺少目标必填分项 '+key)
        unit=str(item.get('unit') or '')
        units=box.get('allowedUnits') or []
        if ((units and unit not in units) or (not units and unit!=str(box.get('unit') or ''))
                or (box.get('unitRequired') and not unit)):
            raise ValueError('源与目标分项 '+key+' 的单位无法无损对应')
        selected={**box,'unit':unit}
        value=str(item['value'])
        options=[o for o in box.get('options') or [] if str(o.get('value'))==value]
        if len(options)>1 or (item.get('valueId') not in (None,'') and len(options)!=1):
            raise ValueError('目标模板未能唯一核实分项 '+key+' 的枚举值 '+value)
        inputs.append(selected); values.append(value)
    props,display=build_props({'inputs':inputs},values)
    if display!=alias:
        raise ValueError('目标结构化填写框生成的展示值与原始别名不一致')
    return props


def _validate_reference(frozen,target_schema):
    if _shop(target_schema.get('shopIdentity') or {})!=frozen['targetShop']:
        raise ValueError('类目模板缺少目标店铺身份或属于另一店铺')
    category=(frozen['sourceDetail']['productInfo'].get('categoryDetail') or {}).get('lastCategoryId')
    if not category or str(target_schema.get('categoryId') or (target_schema.get('category') or {}).get('id'))!=str(category):
        raise ValueError('目标创建模板与来源商品类目不一致')
