import json
from pathlib import Path
import subprocess


SERVER = Path("rsshub/server.mjs")


def _node_eval(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module", "--eval", script, *args],
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_server_is_private_bounded_and_uses_rsshub_public_api() -> None:
    source = SERVER.read_text()
    assert 'host: "127.0.0.1"' in source
    assert 'pathname === "/healthz"' in source
    assert 'pathname.startsWith("/twitter/user/")' in source
    assert "await init(process.env)" in source
    assert "await request(" in source
    assert "dist/index.mjs" not in source
    assert "MAX_URL_LENGTH" in source
    assert "MAX_CONCURRENT_REQUESTS" in source
    assert "TWITTER_AUTH_TOKEN" not in source.replace(
        'process.env["TWITTER_AUTH_TOKEN"]', ""
    )
    assert 'process.argv[2] === "--refresh-query-ids"' in source
    assert "await handle.chmod(mode & 0o777)" in source
    assert "process.chdir(dirname(fileURLToPath(import.meta.url)))" in source
    serving = source[source.index("async function start()") :]
    assert "patchRssHubQueryIds" not in serving.split(
        "const invokedPath", 1
    )[0]


def test_extract_query_ids_requires_both_supported_operations() -> None:
    script = """
      import { extractQueryIds } from './rsshub/server.mjs';
      const bundle = process.argv[1];
      try { console.log(JSON.stringify(extractQueryIds(bundle))); }
      catch { process.exit(7); }
    """
    valid = (
        'queryId:"Abc_def-1234567890",operationName:"UserByScreenName";'
        'queryId:"Zyx_987-1234567890",operationName:"UserTweets";'
    )
    result = _node_eval(script, valid)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "UserByScreenName": "Abc_def-1234567890",
        "UserTweets": "Zyx_987-1234567890",
    }

    missing = _node_eval(script, valid.split(";")[0])
    assert missing.returncode == 7


def test_extract_query_ids_rejects_invalid_or_ambiguous_ids() -> None:
    script = """
      import { extractQueryIds } from './rsshub/server.mjs';
      try { extractQueryIds(process.argv[1]); process.exit(0); }
      catch { process.exit(9); }
    """
    invalid = (
        'queryId:"../secret",operationName:"UserByScreenName";'
        'queryId:"Zyx_987-1234567890",operationName:"UserTweets";'
    )
    ambiguous = (
        'queryId:"Abc_def-1234567890",operationName:"UserByScreenName";'
        'queryId:"Other_def-1234567890",operationName:"UserByScreenName";'
        'queryId:"Zyx_987-1234567890",operationName:"UserTweets";'
    )

    assert _node_eval(script, invalid).returncode == 9
    assert _node_eval(script, ambiguous).returncode == 9


def test_replaces_only_supported_fallback_ids_and_fails_closed() -> None:
    script = """
      import { replaceFallbackIds } from './rsshub/server.mjs';
      const source = `const fallbackIds = {
        UserTweets: "OldTweets123456789",
        UserByScreenName: "OldUser123456789",
        UserTweetsAndReplies: "KeepReplies123456"
      };`;
      try {
        console.log(replaceFallbackIds(source, {
          UserTweets: "NewTweets123456789",
          UserByScreenName: "NewUser123456789"
        }));
      } catch { process.exit(11); }
    """
    result = _node_eval(script)

    assert result.returncode == 0, result.stderr
    assert 'UserTweets: "NewTweets123456789"' in result.stdout
    assert 'UserByScreenName: "NewUser123456789"' in result.stdout
    assert 'UserTweetsAndReplies: "KeepReplies123456"' in result.stdout

    ambiguous = """
      import { replaceFallbackIds } from './rsshub/server.mjs';
      const source = `const fallbackIds = { UserTweets: "OldTweets123456789" };
      const fallbackIds = { UserByScreenName: "OldUser123456789" };`;
      try { replaceFallbackIds(source, {}); process.exit(0); }
      catch { process.exit(12); }
    """
    assert _node_eval(ambiguous).returncode == 12


def test_sources_disable_replies_and_retweets() -> None:
    config = json.loads(Path("config/sources.json").read_text())
    assert [item["handle"] for item in config["sources"]] == [
        "OpenAI",
        "OpenAIDevs",
        "thsottiaux",
        "sama",
    ]
    assert all(item["include_replies"] is False for item in config["sources"])

    client = Path("src/codex_quota_monitor/feed.py").read_text()
    assert "includeRts=0" in client
