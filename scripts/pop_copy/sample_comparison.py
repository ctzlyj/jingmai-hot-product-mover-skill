"""Read a verified public product image from the original source."""
from io import BytesIO
from urllib.parse import urlsplit
import re


def jd_image_url(value):
    value = str(value or '')
    if value.startswith('jfs/'):
        value = 'https://img10.360buyimg.com/imgzone/' + value
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not (parts.hostname or '').endswith('.360buyimg.com') or
            parts.username or parts.password or parts.port not in (None,443) or
            parts.query or parts.fragment or not re.fullmatch(r'/[\w/.-]+', parts.path) or
            '..' in parts.path.split('/') or '/jfs/' not in parts.path):
        raise ValueError('规格图地址未能核实为京东图片空间原文件')
    return value


def download_image(url):
    import requests
    from PIL import Image
    with requests.get(jd_image_url(url), stream=True, allow_redirects=False, timeout=(4,8)) as response:
        if response.status_code != 200:
            raise ValueError('京东图片读取失败')
        raw = BytesIO()
        for chunk in response.iter_content(65536):
            raw.write(chunk)
            if raw.tell() > 12*1024*1024:
                raise ValueError('原图超过单张读取范围')
    raw.seek(0)
    with Image.open(raw) as image:
        image.verify()
    return raw.getvalue()
