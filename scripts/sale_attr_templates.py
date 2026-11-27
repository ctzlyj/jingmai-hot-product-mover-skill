"""Structured sale-attribute templates (templateSkuProps).

Platform pre-validation does NOT check the alias of a structured sale attribute; only the real
submission does (1010300506). So the template must be known BEFORE publishing:

1. official GET /categories/{categoryId}/sale-attr-templates
2. denied / empty -> read-only look at on-sale products of the SAME shop and SAME last category and
   copy only the input-box structure (ids, units) that every sample agrees on; sample values are never copied
3. still unknown -> identify the missing target control; the host can inspect the current editor

Provenance (source product id, category, attribute, input ids, evidence) is recorded; no credentials.
"""
from __future__ import annotations
import json

ON_SALE = (1, 103)


def _loads(value):
    return json.loads(value) if isinstance(value, str) else value


def _leaves(node):
    """Flatten a preprocessing value tree into leaf options {valueId,value}."""
    out = []
    for item in node or []:
        children = item.get('children')
        if children:
            out += _leaves(children)
        elif item.get('id') is not None and item.get('name') is not None:
            out.append({'valueId': item['id'], 'value': item['name']})
    return out


def from_official(rows, attr_id):
    matched=None
    for row in rows or []:
        if str(row.get('templateType')) != '2':
            continue
        for entry in _loads(row.get('templateData')) or []:
            if str(entry.get('attrId')) != str(attr_id):
                continue
            rules = _loads(entry.get('valueRules')) or {}
            inputs = []
            for item in rules.get('items') or []:
                ids = ((item.get('properties') or {}).get('id') or {}).get('enum') or []
                if len(ids)!=1:
                    return None  # One ambiguous box invalidates this template as a source of binding IDs.
                enums = ((item.get('properties') or {}).get('value') or {}).get('enum') or []
                options = []
                for e in enums:
                    if isinstance(e,dict):
                        options += _leaves(e.get('preprocessingSaleAttrValTemplateValueList'))
                features = item.get('features') or {}
                box={'id': ids[0], 'text': features.get('text') or '', 'locked': str(features.get('isLocked')).lower() in ('1','true'), 'options': options}
                unit_rule=(item.get('properties') or {}).get('unit') or {}
                if isinstance(unit_rule,dict):
                    units=unit_rule.get('enum') or []
                    if len(units)==1:box['unit']=str(units[0])
                    elif units:box['allowedUnits']=[str(u) for u in units]
                    elif unit_rule.get('const') is not None:box['unit']=str(unit_rule['const'])
                if 'unit' in (item.get('required') or []):box['unitRequired']=True
                inputs.append(box)
            if inputs:
                minimum=rules.get('minItems')
                if type(minimum) is int and 0<=minimum<=len(inputs):
                    for index,box in enumerate(inputs):
                        if index>=minimum:box['optional']=True
                template={'attrId':str(attr_id),'title':rules.get('title') or '','inputs':inputs,
                          'source':{'kind':'official-interface','attrName':rules.get('title') or '','evidence':'sale-attr-templates'}}
                if matched is not None and matched!=template:return None
                matched=template
    return matched


def _shape(sale_attr):
    return tuple((p.get('id'), p.get('unit') or '') for p in sale_attr.get('templateSkuProps') or [])


def from_same_shop_products(products, category_id, attr_id):
    """Only on-sale products of the same last category; all samples must agree on the input structure."""
    found = []
    for product in products or []:
        info = product.get('productInfo') or {}
        if int((info.get('categoryDetail') or {}).get('lastCategoryId') or 0) != int(category_id):
            continue
        # apply bodies carry a draft-state object; the live status comes from the product list and is stamped by the fetcher
        if int(product.get('liveProductStatus') or 0) not in ON_SALE:
            continue
        for sku in product.get('skuList') or []:
            for attr in sku.get('saleAttrs') or []:
                if str(attr.get('attrId')) == str(attr_id) and attr.get('templateSkuProps'):
                    found.append((info.get('productId'), attr))
    if not found:
        return None
    shapes = sorted({_shape(a) for _, a in found}, key=len)
    longest = shapes[-1]
    # a real template has optional trailing boxes (e.g. "layers"): shorter samples must be a prefix of the longest
    if any(shape != longest[:len(shape)] for shape in shapes):
        return None
    pid, attr = next((pid_, a) for pid_, a in found if _shape(a) == longest)
    required_boxes = len(shapes[0])
    inputs = [{'id': p['id'], 'text': '', 'locked': False, 'options': [], 'optional': i >= required_boxes,
               **({'unit': p['unit']} if p.get('unit') else {})} for i, p in enumerate(attr['templateSkuProps'])]
    return {'attrId': str(attr_id), 'title': attr.get('attrName') or '', 'inputs': inputs,
            'source': {'kind': 'same-shop-same-category-product', 'productId': pid, 'categoryId': int(category_id), 'attrName': attr.get('attrName') or '',
                       'inputIds': [i['id'] for i in inputs], 'optionalInputIds': [i['id'] for i in inputs if i['optional']], 'samples': len(found),
                       'evidence': f"{len(found)} on-sale product sale-attribute(s) in the same shop and category; shorter forms are prefixes of the longest, so trailing boxes are optional"}}


def structured_components(components):
    return [c for c in components if c.get('componentCode','').startswith('sale-prop-')
            and str((c.get('extra') or {}).get('isStructure','')).lower() in ('1','true')]


def resolve(components, category_id, *, fetch_official, fetch_samples):
    """Returns {'templates': {attrId: template}, 'missing': [...], 'ask': text}."""
    structured = structured_components(components)
    templates, missing = {}, []
    if not structured:
        return {'templates': {}, 'missing': [], 'ask': ''}
    official = None
    try:
        official = fetch_official(category_id)
    except Exception:  # no permission / transient: fall through to same-shop discovery
        official = None
    samples = None
    for component in structured:
        attr_id = component['componentCode'].split('sale-prop-', 1)[1]
        template = from_official(official, attr_id) if official else None
        if not template:
            if samples is None:
                samples = fetch_samples(category_id) or []
            template = from_same_shop_products(samples, category_id, attr_id)
        if template:
            templates[attr_id] = template
        else:
            missing.append({'attrId': attr_id, 'attrName': component.get('componentTitle') or ''})
    ask = ''
    if missing:
        names = '、'.join(m['attrName'] or m['attrId'] for m in missing)
        ask = f'类目 {category_id} 的「{names}」尚未取得可用的填写框。宿主可读取目标商品当前编辑页、同店同类目已通过的申请或已授权的模板来源，核对框名、单位和选项。其他已知规格可先整理成草稿。'
    return {'templates': templates, 'missing': missing, 'ask': ask}


def build_props(template, values):
    inputs = template['inputs']
    minimum = sum(1 for box in inputs if not box.get('optional'))
    if not minimum <= len(values) <= len(inputs):
        raise ValueError(f'该销售属性有 {len(inputs)} 个填写框（至少填 {minimum} 个），收到 {len(values)} 个值')
    props = []
    for box, value in zip(inputs, values):
        value = str(value)
        entry = {'id': box['id'], 'value': value}
        if box.get('options'):
            hit = next((o for o in box['options'] if o['value'] == value), None)
            if hit:
                entry['valueId'] = hit['valueId']
            elif box.get('locked'):
                raise ValueError(f"「{box.get('text') or box['id']}」只能从模板选项里选：{[o['value'] for o in box['options']][:12]}")
        elif box.get('locked'):
            raise ValueError(f"「{box.get('text') or box['id']}」是锁定项且没有可用选项")
        if box.get('unit'):
            entry['unit'] = box['unit']
        props.append(entry)
    return props, alias_for(props)


def alias_for(props):
    """Platform rule: alias = the input values joined (the unit is part of the displayed value)."""
    return ''.join(f"{p['value']}{p.get('unit', '')}" if p.get('unit') else p['value'] for p in props)
