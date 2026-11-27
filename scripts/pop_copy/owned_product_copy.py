"""Freeze and compare one authorized same-merchant POP product across two shop profiles."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from .pop_copy_pairing import (
    _digest,
    _shop,
    _product_id,
    freeze_copy,
    _validate,
    _number,
    _specs,
    _groups,
    _gallery_key,
    _view,
    _spec_signature,
    pair_copy_skus,
    _copy_pairing,
    _resolve_display_aliases,
    pair_frozen_target_skus,
    _template_nodes,
    _structured,
    _target_props,
    _validate_reference,
)
from .pop_copy_pairing import PUBLIC_GROUP
from .safety import file_sha
from .pop_copy_pairing import sku_rows


def plan_copy(frozen):
    _validate(frozen)
    content=_view(frozen['sourceDetail'],frozen.get('sourceStocks'),frozen.get('sourceVideos'))
    urls=[]
    for groups in (content['mainGroups'],content['rectangleGroups']):
        for group in (groups or {}).values():
            urls.extend(group)
    for sku in content['skus'].values():
        urls.extend(sku['specImages'] or [])
        urls.extend(sku['logo'] or [])
    for html in content['details'].values():
        if isinstance(html,str):
            urls.extend(_Detail(html).images)
    return {'status':'planned','sourceShop':frozen['sourceShop'],'targetShop':frozen['targetShop'],
            'sourceProductId':frozen['sourceProductId'],'sourceFingerprint':frozen['sha256'],
            'content':content,'skuCount':len(content['skus']),'imageUrls':list(dict.fromkeys(u for u in urls if u)),'writes':0}


def local_copy_draft(frozen):
    """A reviewable product draft from frozen observations; no target validation or media transfer."""
    plan=plan_copy(frozen)
    content=plan['content']
    evidence=[]
    follow=[]
    def need(field,reason,action):
        evidence.append({'field':field,'stage':'before-exact-publication','owner':'host',
                         'reason':reason,'nextAction':action})
    for field in ('title','categoryId','brandId','itemNumber','attributes'):
        if content[field] is None:
            need(field,'源商品未返回该字段','核对源商品当前编辑页或原发布回执，再对照目标类目组件。')
    if content['packSpecification'] is None:
        need('packSpecification','后台未返回包装规格描述','宿主按目标类目核对包装规格字段。此字段没有给出箱内物品清单。')
    if content['packSpecification'] not in (None,'',[]):
        need('packSpecification','已保留源字段原值，包装字段含义和目标写法待核','对照原商品编辑标签和两店当前类目模板。')
    for field,value in content['packaging'].items():
        if value is None:
            need('packaging.'+field,'原包装尺寸或重量未核验','从本商品原资料补齐数值与单位。')
    for channel,html in content['details'].items():
        if html is None:
            need('details.'+channel,'原详情未返回','分别核对源商品电脑端和移动端详情。')
    rows=[]
    for sid,sku in content['skus'].items():
        row={'sourceSkuId':sid,**deepcopy(sku)}
        for group_field,key,position in (('mainGroups','mainGroup','mainImages'),('rectangleGroups','rectangleGroup','rectangleImages')):
            groups=content[group_field]
            row[position]=None if groups is None else [] if not groups else groups.get(sku[key])
            if row[position] is None:
                need(f'sku.{sid}.{position}','图组归属或位置未核验','按原 SKU 和销售属性核对源图组，不合并各款序列。')
        for key,label,action in (
                ('merchantSku','商家编码','读取原 SKU 商家编码，保留前导零及重复值。'),
                ('specifications','销售规格','读取完整销售属性与原值，按当前目标类目核对填写框。'),
                ('price','京东价','核验源 SKU 的京东价。'),('stock','库存','查询同店原 SKU 库存。'),
                ('upcCode','条码','查询原条码及目标类目的条码要求。'),
                ('specImages','规格图','查询原 SKU 规格图及实际归属。'),
                ('logo','缩略图','核验 SKU 当前缩略图及其与图组首位的关系。'),
                ('videoIds','主图视频','查询原 SKU 视频素材关系，区分有、无与尚未查到。')):
            if sku[key] is None:
                need(f'sku.{sid}.{key}',label+'未核验',action)
        if sku['specImages']:
            follow.append({'action':'bind-spec-image-after-target-sku-id','sourceSkuId':sid,'sourceUrls':sku['specImages']})
        if sku['videoIds']:
            follow.append({'action':'verify-and-bind-main-video','sourceSkuId':sid,'sourceVideoIds':sku['videoIds']})
        rows.append(row)
    codes={}
    for row in rows:
        if row['merchantSku'] not in (None,''):
            codes.setdefault(row['merchantSku'],[]).append(row['sourceSkuId'])
    return {'status':'local-draft-ready','intent':'local-draft','sourceShop':frozen['sourceShop'],
            'targetShop':frozen['targetShop'],'sourceProductId':frozen['sourceProductId'],'sourceFingerprint':frozen['sha256'],
            'product':{k:v for k,v in content.items() if k!='skus'},'skuRows':rows,
            'currentStage':{'completed':'local-draft','merchantDecisions':[],
                            'summary':'本次只看草稿，可直接交付。已知值按原商品保留。发布前的核查留给宿主接着查。'},
            'merchantCodeHandling':'按每个原 SKU 保留源店编码，包含重复值。没有本次改码交办或目标店明确的冲突依据时，沿用原值。',
            'repeatedSourceMerchantCodes':{code:ids for code,ids in codes.items() if len(ids)>1},
            'sourceImageUrls':plan['imageUrls'],'missing':[a['field'] for a in evidence],
            'evidenceActions':evidence,'beforePublication':['核对目标店当前类目模板和可售品牌',
                '核对目标店的运费、时效、退换与支付规则','核对各图位对应的目标媒体回执'],
            'followUp':follow,'approvalFingerprint':None,
            'publicationChecked':False,'platformWrites':0}


def source_fidelity(frozen):
    """Stable observations used to approve *this* sample, excluding observation timestamps."""
    content=plan_copy(frozen)['content']
    detail=frozen['sourceDetail']
    product=detail['productInfo']
    desc=product.get('productDetailDesc')
    desc=desc if isinstance(desc,dict) else {}
    delivery={'product':deepcopy(product.get('logisticsInfo')),
              'features':sorted((deepcopy(r) for r in product.get('features') or []
                                 if r.get('key') in ('is7ToReturn','isPayFirst')),key=_digest),
              'skus':{}}
    sale_inputs={}
    for sid,row in sku_rows(detail,frozen['sourceProductId']).items():
        delivery['skus'][sid]={key:deepcopy(row[key]) for key in ('logistics','logisticsInfo') if key in row}
        sale_inputs[sid]=sorted(({key:deepcopy(a[key]) for key in
                                  ('dim','indexId','attrId','attrValues','attrValueAlias','attrName','attrValuesSeqNo','templateSkuProps')
                                  if key in a} for a in row.get('saleAttrs') or []),key=_digest)
    return {'sourceShop':frozen['sourceShop'],'targetShop':frozen['targetShop'],'content':content,
            'detailFlags':{key:desc[key] for key in ('zhuangBaUseFlag','mobileZhuangBaUseFlag') if key in desc},
            'deliverySelections':delivery,'saleInputs':sale_inputs}


def _media(frozen,evidence,kind='images'):
    pairs={}
    if not evidence:
        return pairs
    if (_shop(evidence.get('sourceShop') or {})!=frozen['sourceShop'] or _shop(evidence.get('targetShop') or {})!=frozen['targetShop']
            or str(evidence.get('sourceProductId'))!=frozen['sourceProductId']):
        raise ValueError('媒体迁移证据与本次来源商品或目标店铺不一致')
    for item in evidence.get(kind,[]):
        receipt=item.get('uploadReceipt') or {}
        old,new=(item.get('sourceUrl'),receipt.get('imgUrl')) if kind=='images' else (
            str(item.get('sourceVideoId') or ''),str(receipt.get('videoId') or ''))
        digest=item.get('sourceSha256')
        if (not old or not new or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest)
                or receipt.get('sha256')!=digest or receipt.get('status') not in ('accepted','succeeded')
                or _shop(receipt.get('shopIdentity') or {})!=frozen['targetShop']
                or not item.get('sourceFile') or file_sha(Path(item['sourceFile']))!=digest):
            raise ValueError('图片替换缺少同一文件的来源哈希和目标店铺上传回执')
        pairs.setdefault(old,set()).add(new)
    return pairs


class _Detail(HTMLParser):
    def __init__(self,html):
        super().__init__(convert_charrefs=False)
        self.parts=[]
        self.images=[]
        self.feed(html)
        self.close()

    def handle_starttag(self,tag,attrs):
        values=[]
        for name,value in attrs:
            if tag=='img' and name in ('src','data-src'):
                self.images.append(value)
                value=('image',len(self.images))
            values.append((name,value))
        self.parts.append(('tag',tag,sorted(values,key=lambda x:x[0])))

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag!='img':
            self.handle_endtag(tag)

    def handle_endtag(self,tag):self.parts.append(('end',tag))
    def handle_data(self,data):self.parts.append(('text',data))
    def handle_entityref(self,name):self.parts.append(('entity',name))
    def handle_charref(self,name):self.parts.append(('char',name))
    def handle_comment(self,data):self.parts.append(('comment',data))

    def handle_decl(self,data):self.parts.append(('declaration',data))
    def handle_pi(self,data):self.parts.append(('instruction',data))

    def remap(self, mapping):
        html=[]
        for kind,*args in self.parts:
            value=args[0]
            if kind=='tag':
                attrs=[]
                if value=='img' and not any(isinstance(v,tuple) for _,v in args[1]):
                    raise ValueError('详情图片没有可核对的 src 或 data-src 源地址')
                for name,attr in args[1]:
                    if (name in ('srcset','poster') or (value!='img' and name in ('src','data-src'))
                            or (value=='img' and name.startswith('data-') and isinstance(attr,str) and 'jfs/' in attr)
                            or (name=='style' and 'url(' in str(attr).lower())):
                        raise ValueError('详情含其他媒体引用，需要按原详情核对资源')
                    if isinstance(attr,tuple):
                        attr=mapping[self.images[attr[1]-1]]
                    attrs.append(' '+name+('' if attr is None else '="'+escape(attr,quote=True)+'"'))
                html.append('<'+value+''.join(attrs)+'>')
            elif kind=='end':html.append('</'+value+'>')
            elif kind=='text':html.append(value)
            elif kind=='entity':html.append('&'+value+';')
            elif kind=='char':html.append('&#'+value+';')
            elif kind=='comment':html.append('<!--'+value+'-->')
            elif kind=='declaration':html.append('<!'+value+'>')
            elif kind=='instruction':html.append('<?'+value+'>')
        return ''.join(html)


def _check(checks,field,source,target,*,status=None):
    if status is None:
        status='needs-evidence' if source is None or target is None else 'verified' if source==target else 'different'
    checks.append({'field':field,'status':status,'source':source,'target':target})


def _images(checks,field,source,target,media):
    if source is None or target is None:
        _check(checks,field,None,None)
        return
    _check(checks,field+'.count',len(source),len(target))
    for pos,(old,new) in enumerate(zip(source,target),1):
        if not old or not new:
            state='needs-evidence'
        elif old==new or new in media.get(old,set()):
            state='verified'
        elif new in source or old in media or any(new in values for values in media.values()):
            state='different'
        else:
            state='needs-evidence'
        _check(checks,f'{field}[{pos}]',old,new,status=state)


def _detail_url(value):
    # Material-centre keys are valid component values, but HTML resolves them
    # relative to the product page. Use the same official imgzone rendering as
    # the existing publisher. Absolute CDN renditions keep their exact paths.
    from .sample_comparison import jd_image_url
    if isinstance(value,str) and value.startswith('//'):
        value='https:'+value
    return jd_image_url(value)


def _compare_detail_images(checks,field,source,target,media):
    def canonical(value):
        try:return _detail_url(value)
        except (TypeError,ValueError):return None
    approved={}
    for old,values in media.items():
        key=canonical(old)
        if key:
            approved.setdefault(key,set()).update(url for value in values if (url:=canonical(value)))
    want=[canonical(url) for url in source]
    # A relative key actually stored in the target HTML is not a displayable
    # image address, even when the target upload receipt contains the same key.
    actual=[canonical(url) if isinstance(url,str) and url.startswith(('https://','//')) else None for url in target]
    _images(checks,field,want,actual,approved)


def _compare_groups(checks,field,group_key,source,target,pairs,media):
    left,right=source[field],target[field]
    if left is None or right is None:
        _check(checks,field,None,None)
        return
    _check(checks,field+'.count',len(left),len(right))
    if (PUBLIC_GROUP in left)!=(PUBLIC_GROUP in right):
        _check(checks,field+'.public.present',PUBLIC_GROUP in left,PUBLIC_GROUP in right)
    elif PUBLIC_GROUP in left:
        _images(checks,field+'.public',left[PUBLIC_GROUP],right[PUBLIC_GROUP],media)
    for gid,urls in left.items():
        if gid==PUBLIC_GROUP:continue
        owners=[sid for sid,s in source['skus'].items() if s[group_key]==gid]
        label=field+'.'+gid
        destinations={target['skus'][pairs[sid]][group_key] for sid in owners if sid in pairs}
        if not owners or any(sid not in pairs for sid in owners) or None in destinations:
            _check(checks,label+'.ownership',owners,None)
            continue
        if len(destinations)!=1 or PUBLIC_GROUP in destinations:
            _check(checks,label+'.ownership',owners,sorted(destinations),status='different')
            continue
        dest=destinations.pop()
        actual=sorted(sid for sid,s in target['skus'].items() if s[group_key]==dest)
        _check(checks,label+'.ownership',sorted(pairs[sid] for sid in owners),actual)
        _images(checks,label,urls,right.get(dest),media)


def compare_copy(frozen,target_shop,target_detail,*,stock_rows=None,video_rows=None,media_evidence=None,delivery_evidence=None,
                 target_schema=None):
    _validate(frozen,target_shop)
    target_pid=_product_id(frozen['targetShop'],target_detail)
    if target_pid==frozen['sourceProductId']:
        raise ValueError('目标回读仍指向原 SPU，无法证明已复制出新商品')
    source=plan_copy(frozen)['content']
    target=_view(target_detail,stock_rows,video_rows)
    pair_source,pairing,spec_gaps=_copy_pairing(frozen,source['skus'],target['skus'],target_schema)
    pairs=pairing['pairs']
    checks=[]
    media=_media(frozen,media_evidence)
    video_media=_media(frozen,media_evidence,'videos')
    for field in ('title','itemNumber','categoryId','brandId','attributes','packSpecification'):
        _check(checks,field,source[field],target[field])
    for field,value in source['packaging'].items():
        _check(checks,'packaging.'+field,value,target['packaging'][field])
    _check(checks,'skuCount',len(source['skus']),len(target['skus']))
    _check(checks,'matchedSkuCount',len(source['skus']),len(pairs),status='verified' if len(pairs)==len(source['skus']) else 'needs-evidence')
    if target_schema is not None and not spec_gaps and len(pairs)!=len(source['skus']):
        wanted=[_spec_signature(s['specifications']) for s in pair_source.values()]
        actual=[_spec_signature(s['specifications']) for s in target['skus'].values()]
        if None not in wanted and None not in actual and Counter(wanted)!=Counter(actual):
            expected_only=Counter(wanted)-Counter(actual)
            actual_only=Counter(actual)-Counter(wanted)
            def examples(rows,unmatched):
                return {'count':sum(unmatched.values()),'examples':[
                    s['specifications'] for s in rows.values() if _spec_signature(s['specifications']) in unmatched][:3]}
            _check(checks,'targetTemplateSpecificationCensus',examples(pair_source,expected_only),
                   examples(target['skus'],actual_only),status='different')
    code_checks=Counter()
    for sid,other in pairs.items():
        a,b=source['skus'][sid],target['skus'][other]
        for field in ('merchantSku','price','stock','attributes','upcCode','enabled'):
            _check(checks,f'sku.{sid}.{field}',a[field],b[field])
            if field=='merchantSku':code_checks[checks[-1]['status']]+=1
        wanted=pair_source[sid]['specifications']
        status=('needs-evidence' if sid in spec_gaps else
                'different' if pairing['matchedBy'][sid]=='unique-target-template-values' else 'verified')
        _check(checks,f'sku.{sid}.specifications',wanted,b['specifications'],status=status)
        _images(checks,f'sku.{sid}.specImages',a['specImages'],b['specImages'],media)
        _images(checks,f'sku.{sid}.logo',a['logo'],b['logo'],media)
        _images(checks,f'sku.{sid}.mainVideoIds',a['videoIds'],b['videoIds'],video_media)
    for field,key in (('mainGroups','mainGroup'),('rectangleGroups','rectangleGroup')):
        _compare_groups(checks,field,key,source,target,pairs,media)
    for channel in ('desc','mobileDesc'):
        a,b=source['details'][channel],target['details'][channel]
        label='details.'+channel
        if not isinstance(a,str) or not isinstance(b,str):
            _check(checks,label,None,None)
            continue
        a,b=_Detail(a),_Detail(b)
        _check(checks,label+'.markupAndText',_digest(a.parts),_digest(b.parts))
        _compare_detail_images(checks,label+'.images',a.images,b.images,media)
    from .shop_copy_delivery import compare_delivery
    delivery=compare_delivery(frozen['sourceDetail'],target_detail,frozen['sourceShop'],frozen['targetShop'],pairs,delivery_evidence)
    beyond_templates=outside_creation_templates(frozen,target_schema)
    excluded={c['field'] for c in checks if c['field'] in beyond_templates and c['source'] is None and c['target'] is None}
    checks=[c for c in checks if c['field'] not in excluded]
    states={c['status'] for c in checks}|{delivery['status']}
    status='different' if 'different' in states else 'verified' if states=={'verified'} else 'needs-evidence'
    return {'status':status,'checks':checks,'skuPairs':pairs,'skuPairingEvidence':pairing,'specificationEvidenceGaps':spec_gaps,
            'delivery':delivery,'writes':0,'outsideTemplateFields':sorted(excluded),
            'merchantCodeCoverage':{'sourceSkuCount':len(source['skus']),'independentlyPaired':len(pairs),
                                    'verified':code_checks['verified'],'different':code_checks['different'],
                                    'unverified':len(source['skus'])-code_checks['verified']-code_checks['different']},
            'unmatchedSourceSkus':sorted(set(source['skus'])-set(pairs)),
            'unmatchedTargetSkus':sorted(set(target['skus'])-set(pairs.values())),
            'sourceShop':frozen['sourceShop'],'targetShop':frozen['targetShop'],'sourceProductId':frozen['sourceProductId'],
            'targetProductId':target_pid,'sourceFingerprint':frozen['sha256'],'consumerPageStatus':'unchecked',
            'packagingListStatus':'needs-evidence','scopeNote':'packSpecification 只核对官方字段原值；包装清单含义、买家页展示须另行核验。'}


def _properties(**values):
    return [{'key':key,'value':value} for key,value in values.items()]


def _component(code,value):
    return {'componentCode':code,'value':value}


def _required(node):
    return node.get('required') is True or any(r.get('validateType')=='required' and
        str(r.get('validateRule')).lower()=='true' for r in node.get('validateRule') or [])


def _attr_components(records,prefix,components,missing,where):
    if records is None:
        missing.append(where+'：源属性未回读')
        return []
    result=[]
    for attr in records:
        code=prefix+str(attr.get('attrId') or '')
        field=components.get(code)
        if field is None:
            missing.append(where+'：目标模板缺少 '+code)
            continue
        values=attr.get('values')
        if not isinstance(values,list) or not values or any(v.get('attrValue') in (None,'') for v in values):
            missing.append(where+'：'+code+' 的源属性取值未核验')
            continue
        ids=[str(v['attrValue']) for v in values]
        allowed={str(opt['value']) for opt in field.get('optionsValueList') or [] if opt.get('value') is not None}
        if allowed and not set(ids)<=allowed:
            missing.append(where+'：目标类目选项不包含 '+code+' 的原值')
        # Preserve per-value units and percentage, as in the existing POP editor.
        props={'attrValues':','.join(ids) if prefix=='prop-product-' else '['+','.join(ids)+']'}
        for key,out in (('expand','expands'),('unit','units')):
            if any(v.get(key) not in (None,'') for v in values):
                props[out]=','.join('' if v.get(key) is None else str(v[key]) for v in values)
        result.append({'componentCode':code,'properties':_properties(**props)})
    return result


def _prepare_schema(frozen,reference,media,delivery):
    content=plan_copy(frozen)['content']
    product=frozen['sourceDetail']['productInfo']
    src_rows=sku_rows(frozen['sourceDetail'],frozen['sourceProductId'])
    nodes=list(_template_nodes((reference.get('schema') or {}).get('componentList') or []))
    catalog={node['componentCode']:node for node in nodes}
    schema=[]; missing=[]; follow=[]; used=set()
    omitted=outside_creation_templates(frozen,reference)
    if len(pair_copy_skus(content['skus'],content['skus'])['pairs'])!=len(content['skus']):
        missing.append('原 SKU 的完整销售规格无法唯一对应；商家编码不能证明自身归属')
    def add(sequence,code,value,required=False):
        if value is None:
            if required:missing.append(code+'：来源未核验')
        elif code in catalog:
            sequence.append(_component(code,value))
            used.add(code)
        else:
            missing.append(code+'：目标类目模板未声明')
    def asset(url):
        options=media.get(url,set())
        if len(options)!=1:
            missing.append('目标店铺素材地址未唯一核验：'+str(url))
            return None
        return next(iter(options))
    def pictures(gid,urls,*,sku=False):
        items=[]
        for i,url in enumerate(urls,1):
            props={'imgRefId':gid,'colorId':gid,'orderSort':str(i),'imgUrl':asset(url)}
            if sku:props.update(isGgt=int(i==1),isPrimary=int(i==1))
            items.append({'componentCode':'img','properties':_properties(**props)})
        return items

    if not nodes:missing.append('目标店铺当前类目模板缺少 componentList')
    brands={str(b['brandId']) for b in reference.get('brands') or [] if b.get('brandId') is not None}
    if any(str(opt.get('value'))=='-1' and str(opt.get('label') or '').strip()=='无品牌'
           for opt in catalog.get('WareBrand',{}).get('optionsValueList') or []):
        brands.add('-1')
    if content['brandId'] not in brands:
        missing.append('品牌在目标店铺此类目的可用回执待核验')
    add(schema,'WareTitle',content['title'],True)
    add(schema,'WareBrand',int(content['brandId']) if re.fullmatch(r'-?[0-9]+',content['brandId'] or '') else None,True)
    if 'itemNumber' not in omitted and (content['itemNumber']!='' or 'WareItemnum' in catalog):
        add(schema,'WareItemnum',content['itemNumber'],True)
    for name,code in (('length','WareLength'),('width','WareWide'),('height','WareHeight'),('weight','WareWeight')):
        if 'packaging.'+name not in omitted:
            add(schema,code,content['packaging'][name],True)
    packing = content['packSpecification']
    packing_fields = [n for n in (reference.get('schema') or {}).get('componentList',[])
                      if n.get('productDetailField') == 'productInfo.packSpecification']
    if packing not in (None,'',[]):
        if (len(packing_fields)==1 and not packing_fields[0].get('childrenComponents')
                and not packing_fields[0].get('properties') and not packing_fields[0].get('readOnly')
                and isinstance(packing,str) and packing_fields[0]['componentCode'] not in used):
            add(schema,packing_fields[0]['componentCode'],packing)
        else:
            missing.append('packSpecification 有源值，目标模板尚未给出唯一可写的包装规格字段。')
    elif packing is None and 'packSpecification' not in omitted:
        missing.append('packSpecification 源字段未回读，原包装描述待核验')
    attrs=_attr_components(content['attributes'],'prop-product-',catalog,missing,'商品属性')
    if attrs:add(schema,'CategoryGroupList',attrs)
    used.update(a['componentCode'] for a in attrs)
    entries=[]
    for channel,field,flag in (('pc','desc','zhuangBaUseFlag'),('mobile','mobileDesc','mobileZhuangBaUseFlag')):
        html=content['details'][field]
        if not isinstance(html,str):
            missing.append(channel+' 详情原文未回读')
            continue
        parsed=_Detail(html)
        resolved={url:asset(url) for url in parsed.images}
        if any(value is None for value in resolved.values()):continue
        try:
            value=parsed.remap({old:_detail_url(new) for old,new in resolved.items()})
        except (ValueError,KeyError) as error:
            missing.append(str(error));continue
        props={'desc':value,'userAgent':channel}
        source_desc=product.get('productDetailDesc') or {}
        if flag in source_desc:props['zhuangBaUseFlag']=source_desc[flag]
        entries.append({'componentCode':'detail','properties':_properties(**props)})
    if entries:add(schema,'WareEditor',entries)
    values=delivery.get('targetValues') or {}
    if delivery.get('status')!='ready-to-apply':
        missing.append('目标店铺运费、时效或售后支付规则待核验')
    for code in ('WareTransport','WarePromise','WareSevenDayReturn'):
        if code in values:add(schema,code,values[code])
    if 'isPayFirst' in values:
        if 'PayMethodLimit' not in catalog:missing.append('PayMethodLimit：目标模板未声明')
        else:
            schema.append({'componentCode':'PayMethodLimit','properties':_properties(isPayFirst=values['isPayFirst'])})
            used.add('PayMethodLimit')
    target_groups={role:{PUBLIC_GROUP:PUBLIC_GROUP} for role in ('mainGroup','rectangleGroup')}
    group_owners={role:{} for role in target_groups}
    sku_nodes=[]
    for sid,row in content['skus'].items():
        parts=[]
        if f'sku.{sid}.merchantSku' not in omitted and (row['merchantSku']!='' or 'SkuVendorSku' in catalog):
            add(parts,'SkuVendorSku',row['merchantSku'],True)
        add(parts,'SkuJdprice',row['price'],True)
        add(parts,'SkuStockNum',row['stock'],True)
        add(parts,'SkuEnableStatus',None if row['enabled'] is None else str(row['enabled']),True)
        if row['upcCode']:
            barcodes=[code for code in ('SkuWareUpccode','SkuUpcCode') if code in catalog]
            if len(barcodes)==1:add(parts,barcodes[0],str(row['upcCode']))
            else:missing.append('SKU '+sid+' 条码在目标模板的对应组件未唯一核验')
        elif row['upcCode'] is None and f'sku.{sid}.upcCode' not in omitted:
            missing.append('SKU '+sid+' 条码待核验')
        if sid in values.get('SkuPromise',{}):add(parts,'SkuPromise',values['SkuPromise'][sid])
        attrs=_attr_components(row['attributes'],'prop-sku-',catalog,missing,'SKU '+sid)
        if attrs:add(parts,'SkuAttr',attrs)
        sale=[]
        specs=row['specifications']
        if specs is None:
            missing.append('SKU '+sid+' 销售规格不完整')
        else:
            raw={str(a['attrId']):a for a in src_rows[sid].get('saleAttrs') or []}
            for spec in specs:
                attr=raw[spec['attrId']]
                code='sale-prop-'+spec['attrId']
                if code not in catalog:
                    missing.append(code+'：目标类目未核验该销售属性')
                # The new seller's editor mappingIds are local to this application.
                mid=_digest([frozen['sha256'],spec['dimension'],spec['attrId'],spec['value']])[:32]
                order=_number(attr.get('attrValuesSeqNo'),True)
                if order is None:missing.append('SKU '+sid+' 的规格排序待核验')
                props={'attrValueAlias':'['+spec['value']+']','attrName':spec['name'],'mappingId':mid,
                       'index':str(spec['dimension']),'attrValuesSeqNo':order}
                try:
                    structured=_target_props(attr,catalog.get(code,{}),reference)
                    if structured is not None:
                        props['templateSkuProps']=json.dumps(structured,ensure_ascii=False,separators=(',',':'))
                except ValueError as error:
                    missing.append(f'SKU {sid} 销售属性 {code}：{error}')
                sale.append({'componentCode':code,'properties':_properties(**props)})
                if spec['dimension']==1:
                    for role in target_groups:
                        gid=row[role]
                        if gid in (None,PUBLIC_GROUP):continue
                        if (target_groups[role].setdefault(gid,mid)!=mid or group_owners[role].setdefault(mid,gid)!=gid):
                            missing.append('多个源图组无法独立绑定到目标规格：'+sid)
        if sale:add(parts,'VenderCategoryValue',sale)
        gid=row['mainGroup']
        urls=(content['mainGroups'] or {}).get(gid)
        if gid not in target_groups['mainGroup'] or not urls:
            missing.append('SKU '+sid+' 主图组归属或顺序待核验')
        else:
            add(parts,'SkuMainImage',pictures(target_groups['mainGroup'][gid],urls,sku=True))
        if row['specImages'] is None:
            missing.append('SKU '+sid+' 的规格图未回读')
        elif row['specImages']:
            follow.append({'action':'bind-spec-image-after-target-sku-id','sourceSkuId':sid,'merchantSku':row['merchantSku'],
                           'url':asset(row['specImages'][0])})
        if row['videoIds'] is None:
            missing.append('SKU '+sid+' 的主图视频未回读')
        elif row['videoIds']:
            follow.append({'action':'verify-and-bind-main-video','sourceSkuId':sid,'sourceVideoIds':row['videoIds']})
        if row['logo'] is None:
            missing.append('SKU '+sid+' 的原缩略图未回读')
        elif row['logo'] and row['logo']!=(urls or [])[:1]:
            # The ordinary creation schema derives the SKU logo from its gallery head.
            missing.append('SKU '+sid+' 的缩略图与图组首位不同，需核对目标类目的独立缩略图写法')
        sku_nodes.append(_component('SKU',parts))
        supplied={n['componentCode'] for n in [*parts,*attrs,*sale]}
        used.update(supplied)
        sku_template=next((n for n in nodes if n['componentCode']=='SKU'),{})
        for n in _template_nodes(sku_template.get('childrenComponents') or []):
            if _required(n) and n['componentCode'] not in supplied and n['componentCode'] not in ('img','SKU'):
                missing.append('SKU '+sid+' 缺少目标必填组件 '+n['componentCode'])
    add(schema,'SKUS',sku_nodes,True)
    for key,role,code in (('mainGroups','mainGroup','WareSkuImg'),('rectangleGroups','rectangleGroup','LongImg')):
        groups=content[key]
        if groups is None:
            missing.append(key+' 源图组不完整')
            continue
        items=[]
        for gid,urls in groups.items():
            if gid not in target_groups[role]:
                missing.append(key+' 图组未绑定到明确规格：'+gid)
            else:items.extend(pictures(target_groups[role][gid],urls))
        if items:add(schema,code,items)
    for n in nodes:
        code=n['componentCode']
        if _required(n) and code not in used and code not in ('SKU','img','detail'):
            missing.append('目标类目必填组件未从源事实生成：'+code)
    return schema,list(dict.fromkeys(missing)),follow


def outside_creation_templates(frozen, target_schema):
    """Only omit unreported fields that neither shop's current category template exposes."""
    if not frozen.get('sourceComponentCodes') or not target_schema:
        return set()
    target_codes = {n['componentCode'] for n in _template_nodes(target_schema['schema']['componentList'])}
    if not target_codes:
        return set()
    available = set(frozen['sourceComponentCodes']) | target_codes
    product = frozen['sourceDetail']['productInfo']
    omitted = set()
    # Field paths, when present in both templates, also identify controls with category-specific names.
    source_paths = frozen.get('sourceDetailFields')
    target_paths = {n.get('productDetailField') for n in _template_nodes(target_schema['schema']['componentList'])
                    if isinstance(n.get('productDetailField'),str)}
    if (source_paths and target_paths and 'productInfo.packSpecification' not in set(source_paths) | target_paths
            and product.get('packSpecification') is None):
        omitted.add('packSpecification')
    for field, code, keys in (
        ('packaging.length','WareLength',('length',)), ('packaging.width','WareWide',('wide','width')),
        ('packaging.height','WareHeight',('height',)), ('packaging.weight','WareWeight',('weight',)),
        ('itemNumber','WareItemnum',('itemNum',)),
    ):
        if code not in available and all(product.get(key) is None for key in keys):
            omitted.add(field)
    for sid, row in sku_rows(frozen['sourceDetail'],frozen['sourceProductId']).items():
        for field, key, codes in (('merchantSku','outerId',{'SkuVendorSku'}),
                                  ('upcCode','upcCode',{'SkuUpcCode','SkuWareUpccode'})):
            if available.isdisjoint(codes) and row.get(key) is None:
                omitted.add(f'sku.{sid}.{field}')
    return omitted


def prepare_copy_schema(frozen,target_shop,target_schema,*,media_evidence=None,delivery_evidence=None):
    """Produce the exact category payload candidate. This function never submits it."""
    _validate(frozen,target_shop)
    _validate_reference(frozen,target_schema)
    source=frozen['sourceDetail']
    from .shop_copy_delivery import compare_delivery
    delivery=compare_delivery(source,None,frozen['sourceShop'],frozen['targetShop'],{},delivery_evidence)
    schema,missing,follow=_prepare_schema(frozen,target_schema,_media(frozen,media_evidence),delivery)
    return {'status':'needs-evidence' if missing else 'ready','schema':schema,'missing':missing,'followUp':follow,
            'delivery':delivery,'outsideTemplateFields':sorted(outside_creation_templates(frozen,target_schema)),
            'sourceFingerprint':frozen['sha256'],'targetShop':frozen['targetShop'],'writes':0}
