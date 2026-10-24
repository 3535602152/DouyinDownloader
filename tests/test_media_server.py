from urllib.request import urlopen

from gui.media_server import LocalMediaServer


def test_media_server_allows_cross_origin_reads(tmp_path):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xd9")
    server = LocalMediaServer()
    try:
        with urlopen(server.url(cover), timeout=5) as response:
            assert response.read() == cover.read_bytes()
            assert response.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        server.close()
