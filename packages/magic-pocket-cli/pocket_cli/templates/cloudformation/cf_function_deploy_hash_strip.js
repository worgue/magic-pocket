function handler(event) {
    var request = event.request;
    var prefix = '{{ prefix }}';
    // viewer が同名 header を送ってもキャッシュキーに使わせない
    delete request.headers['{{ hash_header }}'];
    if (request.uri.indexOf(prefix) === 0) {
        var rest = request.uri.substring(prefix.length);
        var slash = rest.indexOf('/');
        if (slash > 0) {
            var segment = rest.substring(0, slash);
            if ({{ segment_condition }}) {
                request.uri = prefix + rest.substring(slash + 1);
                // URI から外した hash をキャッシュキーに残す (CachePolicy が参照する)
                request.headers['{{ hash_header }}'] = { value: segment };
            }
        }
    }
    return request;
}
