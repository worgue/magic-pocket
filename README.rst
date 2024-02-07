=======================
magic-pocket
=======================

``magic-pocket`` は、ウェブアプリケーションの環境を構築するためのツールです。
複数の環境を追加コスト無しで構築することに重点が置かれています。
現在、以下の組み合わせで、djangoアプリケーションを迅速にupすることができます。
- Djago環境: AWS lambda(cotainer) through cloudfront
- データベース: neon
- キャッシュ: AWS EFS
- フロントエンド: AWS lambda or S3 through cloudfront
- staticfile: AWS S3 through cloudfront
- mediafile(public): AWS S3 through cloudfront
- mediafile(private): AWS S3 through cloudfront

Key Features
------------

- toml settings
- deploy and cleanup commands


Commands
--------

- magic-pocket dev neon status|create|delete|cleanup
- magic-pocket dev lambda status|create|delete|cleanup|update
- magic-pocket dev deploy
- magic-pocket dangerouse dev destroy-all
- magic-pocket dev update
- magic-pocket dev django lambda-manage [command]
  - e.g) magic-pocket dev django lambda-manage showmigrations
