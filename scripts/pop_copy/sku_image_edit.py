"""Discover the verified target shop material zone for product copy."""
def media_space(api):
    zones = api.get('jdo-product-material', '/rest/sp-product/v0/media-zones').get('data') or []
    zone = next((z['zoneId'] for z in zones if z.get('zoneType') == 1), None)
    if zone is None:
        raise ValueError('未找到本店图片空间')
    cats = api.get('jdo-product-material', '/rest/sp-product/v0/media-zones/{zoneId}/categories',
                   {'zoneId':zone, 'page':1, 'pageSize':100}).get('data') or []
    cat = next((c['categoryId'] for c in cats if c.get('categoryName') == '我的本地文件' or c.get('status') == 1), None)
    if cat is None:
        raise ValueError('本店图片空间无可用目录')
    return zone, cat
