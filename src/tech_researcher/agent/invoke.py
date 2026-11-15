"""② ④ の AI の呼び出しの境界（structure.md 2.2・3.3・8.3）。AI の差し替えはここで行う。

agent コンテナの Claude Code を `docker compose run --rm -T agent claude -p …` で呼び、標準出力の
JSON から構造化出力（structured_output）を取り出す処理は #18 で実装する。それまでは呼べないので、
統括スクリプトは ② を実行エラー（not_implemented）にする。テストでは偽の実装に差し替える
（製品には偽の AI のモードを作らない）。
"""


def call_agent(body: bytes, timeout_s: float) -> object:
    """入力の JSON（標準入力に渡すバイト列）を AI に渡し、構造化出力を返す。

    timeout_s は段階の上限（limits の analyze。② は再試行を含めた合計。D-21）。返す値の形は
    確かめない（③ が D6(a) で検査する）。
    """
    raise NotImplementedError
