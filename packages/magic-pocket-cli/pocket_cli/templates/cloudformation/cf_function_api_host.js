function handler(event) {
    var request = event.request;
    request.headers['x-forwarded-host'] = { value: request.headers.host.value };
    // event.viewer.ip は CloudFront が TCP 接続から取得する viewer IP で詐称不可。
    // viewer が同名 header を送っても上書きするため、origin は真の client IP を
    // 信頼できる。OriginVerifyMiddleware が REMOTE_ADDR に正規化する。
    request.headers['x-pocket-viewer-ip'] = { value: event.viewer.ip };
    return request;
}
