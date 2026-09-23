(description = null, pricePolicy = {}) => {
  const pageUrl = new URL(location.href);
  if (!['item.taobao.com', 'detail.tmall.com'].includes(pageUrl.hostname) || !pageUrl.searchParams.get('id')) {
    return {ok: false, reason: 'NOT_A_PRODUCT_PAGE'};
  }
  const response = window.__ICE_APP_CONTEXT__?.loaderData?.home?.data?.res;
  if (!response?.item || !response?.skuBase || !response?.skuCore) {
    return {ok: false, reason: 'PRODUCT_STATE_UNAVAILABLE'};
  }
  const displayedPrice = response.componentsVO?.priceVO?.price || {};
  const acceptsReturnedPrice = pricePolicy?.acceptPrePromotionReturnedPrice === true;
  if (/登录/.test(displayedPrice.priceActionText || '') && !acceptsReturnedPrice) {
    return {ok: false, reason: 'EFFECTIVE_PRICE_UNVERIFIED'};
  }
  const select = (value, keys) => Object.fromEntries(keys.filter(key => value?.[key] !== undefined).map(key => [key, value[key]]));
  const descriptionUrl = value => {
    try {
      const url = new URL(value, pageUrl);
      if (url.protocol !== 'https:' || url.hostname !== 'market.m.taobao.com' || url.username || url.password ||
          url.pathname !== '/app/detail-project/desc/index.html' ||
          url.searchParams.getAll('id').length !== 1 || url.searchParams.get('id') !== pageUrl.searchParams.get('id')) return '';
      const clean = new URL(url.origin + url.pathname);
      for (const key of ['id', 'descVersion', 'type', 'f', 'sellerType']) {
        if (url.searchParams.has(key)) clean.searchParams.set(key, url.searchParams.get(key));
      }
      const file = clean.searchParams.get('f');
      return file && /^desc\/[A-Za-z0-9_.-]+$/.test(file)
        ? clean.href.replace('f=' + encodeURIComponent(file), 'f=' + file) : clean.href;
    } catch { return ''; }
  };
  const item = select(response.item, ['itemId', 'title', 'images']);
  const linkedUrl = descriptionUrl(response.item.pcADescUrl);
  if (linkedUrl) item.pcADescUrl = linkedUrl;
  item.videos = (response.item.videos || []).map(video => select(video, ['videoId', 'url', 'videoThumbnailURL']));
  const skus = (response.skuBase.skus || []).map(sku => select(sku, ['skuId', 'propPath', 'outerId']));
  const props = (response.skuBase.props || []).map(prop => ({...select(prop, ['pid', 'name']),
    values: (prop.values || []).map(value => select(value, ['vid', 'name', 'image']))}));
  const sku2info = Object.fromEntries(skus.map(sku => {
    const info = response.skuCore.sku2info?.[sku.skuId] || {};
    return [sku.skuId, {...select(info, ['quantity']),
      ...Object.fromEntries(['price', 'subPrice'].filter(key => info[key]).map(key => [key, select(info[key], ['priceMoney'])]))}];
  }));
  let images = [...document.querySelectorAll('[data-name="singleImage"]')].map(image => ({attrs: {
    'data-name': 'singleImage', 'data-src': image.getAttribute('data-src') || image.getAttribute('src') || ''
  }}));
  if (String(item.itemId) !== pageUrl.searchParams.get('id') || !skus.length) {
    return {ok: false, reason: 'IDENTITY_OR_SOURCE_INCOMPLETE'};
  }
  let detailScope = 'current-document';
  if (!images.length && description) {
    if (!linkedUrl || description.ok !== true || descriptionUrl(description.pageUrl) !== linkedUrl) {
      return {ok: false, reason: 'DESCRIPTION_IDENTITY_MISMATCH'};
    }
    images = (description.images || []).map(image => ({attrs: select(image.attrs, ['data-name', 'data-src', 'src'])}));
    detailScope = 'linked-description-page';
  }
  if (!images.length) return linkedUrl ? {ok: false, reason: 'DESCRIPTION_PAGE_REQUIRED', descriptionUrl: linkedUrl}
    : {ok: false, reason: 'IDENTITY_OR_SOURCE_INCOMPLETE'};
  return {ok: true, schemaVersion: 'taobao-browser-capture/v1', capturedAt: new Date().toISOString(),
    ...(acceptsReturnedPrice ? {sourcePriceBasis: 'taobao-returned-price'} : {}),
    pageUrl: `${pageUrl.origin}${pageUrl.pathname}?id=${encodeURIComponent(pageUrl.searchParams.get('id'))}`,
    response: {item, seller: select(response.seller || {}, ['sellerId', 'shopId', 'shopName']), skuBase: {props, skus}, skuCore: {sku2info}},
    skuCount: skus.length, detailImageCount: images.length, detailScope,
    ...(detailScope === 'linked-description-page' ? {detailPageUrl: linkedUrl} : {}), detailDom: {images}};
}
