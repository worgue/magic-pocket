function handler(event) {
    var response = event.response;
    var status = response.statusCode;
    if ((status >= 200 && status < 300) || status === 304) {
        response.headers['cache-control'] = { value: 'public, max-age={{ max_age }}, immutable' };
    }
    return response;
}
