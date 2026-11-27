"""Browser backstop: image decisions in future components cannot hotlink."""
IMAGE_POLICY = "img-src 'self' https://oss.wowdaily.cn data: blob:"


def public_image_policy(get_response):
    def middleware(request):
        response = get_response(request)
        if response.get('Content-Type', '').partition(';')[0].lower() in ('text/html', 'application/xhtml+xml'):
            # Separate policies intersect; never relax sandboxed report documents.
            existing = response.get('Content-Security-Policy')
            response['Content-Security-Policy'] = f'{existing}, {IMAGE_POLICY}' if existing else IMAGE_POLICY
        return response
    return middleware
