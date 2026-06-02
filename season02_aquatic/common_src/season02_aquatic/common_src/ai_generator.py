import os
import time
import json
import re
import random
import google.generativeai as genai
import google.api_core.exceptions


# ============================================================================
# 🔑 チャンネル別メタデータ定義（一元管理テーブル）
# ============================================================================
CHANNEL_METADATA = {
    "dogs_jp": {
        "channel_id": "DOGS_JP",
        "genre": "犬のマニアックな生態雑学・行動の謎",
        "visual_theme": "可愛い犬たちの映像・写真",
    },
    "pets_jp": {
        "channel_id": "PETS_JP",
        "genre": "猫の飼い主しか知らないマニアックなあるある・生体雑学",
        "visual_theme": "可愛い猫たちの映像・写真",
    },
    "aesthetic_en": {
        "channel_id": "AESTHETIC_EN",
        "genre": "地理クイズ（マニアックな3択問題と解説）",
        "visual_theme": "スマートで美しい景観・抽象的な幾何学背景映像",
    },
    "pawvana": {
        "channel_id": "PAWVANA",
        "genre": "猫のマニアックな雑学（海外向け）",
        "visual_theme": "癒やされる猫たちの高画質映像・写真",
    },
    "ham_jp": {
        "channel_id": "HAM_JP",
        "genre": "ハムスターの飼育上の意外なリスク・可愛い仕草の裏の秘密",
        "visual_theme": "小さく愛らしいハムスターの映像・写真",
    },
    "dogs_en": {
        "channel_id": "DOGS_EN",
        "genre": "犬の行動心理・驚きの生態（海外向け）",
        "visual_theme": "躍動感のある犬たちの高画質映像",
    },
    "lgbtq_en": {
        "channel_id": "LGBTQ_EN",
        "genre": "LGBTQの歴史・カルチャー・インスピレーショナルな雑学",
        "visual_theme": "スタイリッシュで多様性を表現するグラデーション・抽象背景",
    },
    "romance_en": {
        "channel_id": "ROMANCE_EN",
        "genre": "ロマンス・人間関係の心理学・深層心理雑学",
        "visual_theme": "エモーショナルでシネマティックな背景・落ち着いた映像",
    },
    "aquatic_en": {
        "channel_id": "AQUATIC_EN",
        "genre": "Fascinating, obscure aquatic & marine life trivia, deep-sea secrets, and freshwater wonders",
        "visual_theme": "Stunning marine life, deep-sea organisms, colorful coral reefs, aquarium scenes, and freshwater ecosystems",
    },
}

# profile_key（フォルダ名 "01_dogs_jp" 等）からメタデータキーを解決するマッピング
_PROFILE_KEY_MAP = {
    "01_dogs_jp": "dogs_jp",
    "02_pets_jp": "pets_jp",
    "03_aesthetic_en": "aesthetic_en",
    "04_pawvana": "pawvana",
    "05_ham_jp": "ham_jp",
    "06_dogs_en": "dogs_en",
    "07_lgbtq_en": "lgbtq_en",
    "08_romance_en": "romance_en",
    "season02_aquatic": "aquatic_en",
}


def _resolve_metadata_key(profile_key):
    """profile_key からメタデータテーブルのキーを解決する。
    フォルダ名 (01_dogs_jp) でも config内キー (dogs_jp) でもマッチ可能。"""
    if profile_key in CHANNEL_METADATA:
        return profile_key
    if profile_key in _PROFILE_KEY_MAP:
        return _PROFILE_KEY_MAP[profile_key]
    # 部分一致フォールバック（例: "dogs_jp" が "01_dogs_jp" の末尾にマッチ）
    for folder_key, meta_key in _PROFILE_KEY_MAP.items():
        if profile_key in folder_key or folder_key in profile_key:
            return meta_key
    return None


def _get_seed_perspective(seed):
    """シード値の下一桁から思考ロジックの視点を決定する。"""
    last_digit = seed % 10
    if last_digit in (0, 1, 2):
        return "warning"  # 意外な落とし穴・警告・NG行動
    elif last_digit in (3, 4, 5):
        return "expert"   # 専門家しか知らないマニアックな仕組み・生体構造
    else:
        return "daily"    # 日常の疑問・なぜ？の解明


def _get_perspective_instruction_ja(perspective):
    """日本語チャンネル用：シード値ベースの思考ロジック指示文を返す。"""
    if perspective == "warning":
        return "「意外な落とし穴・警告・やってはいけないNG行動」の視点で書いてください。飼い主や一般人が気づかない危険性やリスクを前面に出してください。"
    elif perspective == "expert":
        return "「専門家しか知らないマニアックな仕組み・生体構造・メカニズム」の視点で書いてください。プロや研究者だけが知っている裏側の事実を紹介してください。"
    else:
        return "「日常の疑問・なぜ？の解明」の視点で書いてください。誰もが一度は不思議に思ったことがあるような身近な『なぜ？』に答えてください。"


def _get_perspective_instruction_en(perspective):
    """英語チャンネル用：シード値ベースの思考ロジック指示文を返す。"""
    if perspective == "warning":
        return "Write from the perspective of 'Unexpected Pitfalls, Warnings, and Things You Should NEVER Do'. Highlight hidden dangers or risks that most people overlook."
    elif perspective == "expert":
        return "Write from the perspective of 'Expert-Only Knowledge: Obscure Mechanisms and Hidden Structures'. Reveal insider facts that only professionals or researchers know."
    else:
        return "Write from the perspective of 'Everyday Mysteries: Why Does This Happen?'. Answer a relatable, curiosity-driven question that everyone has wondered about at least once."


def generate_viral_script(topic="health", channel_context="", api_key=None, feedback=None, language="en", profile_key="", past_titles=None):
    """
    【完全ガード型プロンプトシステム v2】
    チャンネルメタデータ（GENRE, VISUAL_THEME）とRANDOM_SEEDを動的注入し、
    ジャンル逸脱・王道テーマ重複・世界観崩壊を物理的に防止する。
    台本生成+品質監査を1回のAPI呼び出しで完結。
    最大3回（初回+リトライ2回）、無料枠タイマーを刺激しない安全設計。
    """
    if api_key:
        import json
        import os
        from google.oauth2 import service_account

        service_account_str = os.environ.get("GEMINI_SERVICE_ACCOUNT")
        credentials = None
        if service_account_str:
            try:
                info = json.loads(service_account_str)
                credentials = service_account.Credentials.from_service_account_info(info)
            except Exception:
                if os.path.exists(service_account_str):
                    try:
                        credentials = service_account.Credentials.from_service_account_file(service_account_str)
                    except Exception:
                        pass
        if credentials:
            genai.configure(credentials=credentials)
        else:
            genai.configure(api_key=api_key)

    model = genai.GenerativeModel('gemini-2.0-flash-lite')

    # === メタデータ解決 ===
    meta_key = _resolve_metadata_key(profile_key)
    if meta_key and meta_key in CHANNEL_METADATA:
        meta = CHANNEL_METADATA[meta_key]
        channel_id = meta["channel_id"]
        genre = meta["genre"]
        visual_theme = meta["visual_theme"]
    else:
        # フォールバック: メタデータが見つからない場合は旧方式の値を使用
        print(f"[WARN] No CHANNEL_METADATA found for profile_key='{profile_key}'. Using legacy context.")
        channel_id = profile_key.upper()
        genre = topic
        visual_theme = "general"

    # === シード値生成と思考ロジック分岐 ===
    random_seed = random.randint(0, 99999)
    perspective = _get_seed_perspective(random_seed)
    print(f"[GUARD_SYSTEM] CHANNEL_ID={channel_id}, SEED={random_seed}, PERSPECTIVE={perspective}")

    feedback_section = ""
    if feedback:
        feedback_section = f"""
=== CRITICAL FEEDBACK (YOU MUST FIX THESE POINTS) ===
{feedback}
=====================================================
"""

    past_titles_section = ""
    if past_titles:
        titles_bullet = "\n".join([f"- {t}" for t in past_titles])
        if language == "ja":
            past_titles_section = f"""
=== 🚨重要：絶対に避けるべき過去のテーマ・タイトル履歴 ===
以下のテーマやタイトル、およびこれらに含まれるキーワード（例：「耳」「盲腸糞」など）と重複するトピックは【絶対に避けて】ください。
これまで全く触れていない、完全に新しい切り口・雑学・生体構造のみをテーマに選定してください。
{titles_bullet}
======================================================
"""
        else:
            past_titles_section = f"""
=== 🚨IMPORTANT: PREVIOUSLY USED THEMES/TITLES TO AVOID ===
DO NOT use any themes, titles, or key concepts similar to the following.
Select a completely new, unique angle or trivia that has never been covered before:
{titles_bullet}
======================================================
"""

    # === プロンプト構築 ===
    if language == "ja":
        perspective_instruction = _get_perspective_instruction_ja(perspective)
        is_hamster = "ham" in profile_key.lower() or "hamster" in topic.lower()
        is_dog = "dog" in profile_key.lower() or "inu" in profile_key.lower()
        is_cat = "pet" in profile_key.lower() or "cat" in topic.lower() or "neko" in topic.lower()

        # 結び（オチ）のバリエーションをシード値（random_seed）と動物の種類から選択
        ending_options = []
        if is_dog:
            ending_options = [
                "視聴者の共感を誘う形（例：「あなたの愛犬はどうですか？」「もし見かけたら観察してみてね」など、愛犬に対する自然な問いかけや観察の呼びかけ）",
                "クスッと笑えるオチや感想（例：「ワンコの世界は奥が深い…」「本当に驚きですよね」など、犬の不思議な生態に対する感嘆やユーモア）",
                "コメントを自然に促す別の表現（例：「当てはまる仕草があったらコメントで教えてね」「意外だと思った人は 👍 で教えて！」など、視聴者への自然なリアクション促進）"
            ]
        elif is_cat:
            ending_options = [
                "視聴者の共感を誘う形（例：「あなたの愛猫はどうですか？」「もし見かけたら観察してみてね」など、愛猫に対する自然な問いかけや観察の呼びかけ）",
                "クスッと笑えるオチや感想（例：「猫様の世界は奥が深い…」「本当に驚きですよね」など、猫の不思議な生態に対する感嘆やユーモア）",
                "コメントを自然に促す別の表現（例：「当てはまる仕草があったらコメントで教えてね」「意外だと思った人は 👍 で教えて！」など、視聴者への自然なリアクション促進）"
            ]
        elif is_hamster:
            ending_options = [
                "視聴者の共感を誘う形（例：「あなたのハムちゃんはどうですか？」「もし見かけたら観察してみてね」など、ハムスターに対する自然な問いかけや観察の呼びかけ）",
                "クスッと笑えるオチや感想（例：「ハムスターの世界は奥が深い…」「本当に驚きですよね」など、ハムスターの不思議な生態に対する感嘆やユーモア）",
                "コメントを自然に促す別の表現（例：「当てはまる仕草があったらコメントで教えてね」「意外だと思った人は 👍 で教えて！」など、視聴者への自然なリアクション促進）"
            ]
        else:
            ending_options = [
                "視聴者の共感を誘う形（例：「あなたのペットはどうですか？」「もし見かけたら観察してみてね」など、ペットに対する自然な問いかけや観察の呼びかけ）",
                "クスッと笑えるオチや感想（例：「動物たちの世界は奥が深い…」「本当に驚きですよね」など、不思議な生態に対する感嘆やユーモア）",
                "コメントを自然に促す別の表現（例：「当てはまる仕草があったらコメントで教えてね」「意外だと思った人は 👍 で教えて！」など、視聴者への自然なリアクション促進）"
            ]
        
        selected_ending_style = ending_options[random_seed % len(ending_options)]

        prompt = f"""# あなたの役割と厳格な前提
あなたは与えられた指示を1ミリも違えずに実行する、完全自動化されたスクリプトの一部です。
人間の主観や「一般的なAIの気遣い」は一切不要です。提示されたシステム変数とルールにのみ従ってください。

# 動的システム入力
- 今回のチャンネルID: {channel_id}
- 今回の固定ジャンル: {genre}
- 今回の固定背景世界観: {visual_theme}
- 動的シード値: {random_seed}

# 🚨 日本語チャンネル専用：表現重複の絶対禁止ルール
1. 【同一フレーズの重複使用禁止】
   1本の台本（scene1 + scene2）の中で、同じ意味の問いかけやフレーズ（例：「知ってた？」「〜って知ってる？」「〜なんだよ」など）を2回以上繰り返して使用することを【完全禁止】とします。
   冒頭（scene1）で「〜知ってた？」と問いかけているにもかかわらず、後半や結び（scene2）で再び「みんなは知ってた？」や「知ってた？」等の同じフレーズを繰り返すことは極めて不自然でくどいため、絶対に避けてください。
2. 【後半の結び（オチ）の表現の多様化】
   動画の結び（scene2の後半部分）で機械的に「みんなは知ってた？」と繰り返すのは即座に廃止してください。
   今回の結び部分は、以下の【今回の結びスタイル】に従い、シード値や文脈（{genre}）に合わせて自然な日本語で作成してください。
   【今回の結びスタイル】：{selected_ending_style}

# ガードレール（AIの暴走・重複を物理的に防ぐ絶対命令）
1.【ジャンルと背景の完全一致】
  今回の動画テキストは、必ず「{genre}」および「{visual_theme}」に100%合致するものに限定してください。
  世界観を壊すトピックの越境は「完全禁止」です。
2.【ファーストインプレッションの破棄】
  「{genre}」という単語を聞いて、あなたが脳内で「最初に思いついたトップ3つの王道トピック」は、
  他のチャンネルで既に使用されているため【完全廃棄】してください。4番目以降のマイナーな切り口のみを採用すること。
3.【シード値による強制多様化】
  動的シード値「{random_seed}」の末尾（下一桁）に基づき、以下の思考ロジックを強制適用してください。
  {perspective_instruction}

# 違反時の厳罰
- 上記ジャンルと無関係なテキストを出力した場合、あるいは過去のありふれた重複テキストを出力した場合、
  このシステムはエラーとして処理され、あなたの出力は破棄されます。

{past_titles_section}

{feedback_section}

# 台本の構成ルール（13〜15秒高速テンポ最適化：細切れ構成）
1. 【シーン1（前半約6〜7秒）】：必ず「〜って知ってた？」等の全角疑問符（？）付きの強力なフックから開始し、話題を提示する。4〜5セクション（改行区切りの行）で構成してください。
2. 【シーン2（後半約7〜8秒）】：驚きの事実、理由、裏付けなどをテンポよく伝え、最後に上記の【今回の結びスタイル】に指定された表現を用いて自然な日本語で締める。5〜6セクション（改行区切りの行）で構成してください。
3. 【全体のセクション構成】：台本全体（scene1 + scene2）で必ず【合計9〜11セクション（改行区切りの行）】の細切れブロックにしてください。
4. 【1セクション（1行）の文字数制限】：視聴者がナレーションより先に読み終わって離脱するのを防ぐため、1つのセクション（1行）あたりの文字数は句読点を含めて【12文字〜15文字以内】を絶対に厳守してください。1画面に長い文章をまとめて出すことは厳禁です。
5. 【内容のディテール強化】：切り替え数が増える分、雑学の「理由」や「驚きの裏付け」などの詳細な説明をしっかりと含めて、視聴者を引き込む濃い内容にしてください。
6. 【意味のまとまり（文節・単語）での改行コード（\\n）の挿入】：各セクション内で、文字が長くなる場合や2行に分ける場合は、画面端で単語が不自然にブツ切りになるのを防ぐため、必ず「単語の途中」ではなく、「意味の区切りが良いところ（文節や単語の境界）」に改行コード（\\n）を直接挿入して出力してください。
   （良い例）
   「すり抜けちゃう\\n危険、知ってた？」
   「人間の『1億倍』も\\n鼻が良いんだよ」
   （悪い例：単語の途中で切れるような構成は厳禁）
   「すり抜けち\\nゃう危険、知ってた？」
7. 【改行前後のスペース完全排除】：プログラム側でスペースは自動除去されるため、改行コード（\\n）の前後には絶対に半角・全角スペースを挟まないでください。


# 🎙️ 音声合成（TTS）発音最適化ルール（日本語チャンネル専用・最重要）
1. 【息継ぎの「間（ま）」を読点で物理的に制御】
   音声エンジンが一気にまくし立てて不自然になるのを防ぐため、3〜5文節ごとに必ず読点「、」を打ってください。
   特に以下の位置には意識的に読点を挿入すること：
   - 主語の直後（例：「猫って、実は…」）
   - 接続詞・副詞の直後（例：「実は、」「でも、」「なんと、」）
   - 強調したいフレーズの直前（例：「それが、〜なんだよね」）
   これにより、人間のナレーターが語りかけるような自然なリズムと間が生まれます。
2. 【TTS誤読防止：難読漢字・専門用語のひらがな化】
   音声エンジンが平坦に誤読しやすい漢字や専門用語は、最初からひらがなで出力してください。
   変換必須の例：
   - 「治癒」→「ちゆ」、「軽減」→「けいげん」、「著しい」→「いちじるしい」
   - 「嗅覚」→「きゅうかく」、「盲腸」→「もうちょう」、「顎」→「あご」
   - 「齧る」→「かじる」、「痙攣」→「けいれん」、「蠕動」→「ぜんどう」
   - 「所以」→「ゆえん」、「所謂」→「いわゆる」、「漸く」→「ようやく」
   判断に迷ったら、漢字よりひらがなを優先すること。ひらがなにしても字幕の品質には一切影響しません。
3. 【完全な口語（喋り言葉）のリズムの徹底】
   あなたは「友達にワクワクしながら雑学を教える、テンションの高いナレーター」です。
   書き言葉や論文調は完全に禁止。以下のような歯切れの良い短文・口語表現のみで構成すること：
   - 「〜って知ってた？」「〜なんだよね」「〜らしいんだけど」「〜してるんだって」
   - 「実はこれ、〜なんだよ」「マジで〜なんだよね」「びっくりなんだけど、」
   硬い表現（「〜である」「〜と言われている」「〜が判明した」）は絶対に使用しないこと。

# 厳守ルール
- 【表現重複の絶対禁止】scene1とscene2で「知ってた？」「〜知ってる？」などの同じ問いかけやフレーズを決して重複させないこと。1本の台本内で同じ問いかけを繰り返すことは厳禁です。
- 【超重要・総文字数の絶対上限】音声合成（TTS）の速度を計算に入れ、確実に13〜15秒以内に収まるよう、scene1+scene2の総文字数は必ず「130文字〜150文字以内」（スペース・改行を除く）に厳密に収めてください。これより短すぎたり長くなると強制的に却下されます。
- 【1セクションの文字数制限】各セクション（改行区切りの各行）の文字数は必ず「12文字〜15文字以内」に抑えてください。
- 【セクション数の厳守】scene1+scene2の合計行数（改行で区切られた行数）が「9〜11行」であることを厳守してください。
- 【意味のまとまりでの改行コード（\\n）挿入】各セクション（1行）で文字が長くなる場合や2行に分ける場合は、絶対に単語の途中でブツ切りにせず、意味の区切りの良い部分（文節・単語の境界）に改行コード（\\n）を直接挿入すること。改行コード（\\n）の前後には絶対に半角・全角スペースを挟まないこと。
- 【句読点の必須化と配置】音声の自然な息継ぎのため、3〜5文節ごとに「、」を、各セクション（行）の末尾には「、」または「。」を必ず打ってください。

- 自然な日本語の口語体のみ。絵文字・特殊記号（□、♥、★等）は一切使用しないこと。文字化けの原因になる。
- 【】「」[]などのブラケット記号・括弧類はタイトルに絶対に使用しないこと。
- チャンネルのテーマ「{genre}」と無関係な動物・トピックは絶対に含めないこと。
- 不自然な翻訳調・書き言葉・論文調の日本語は不可。友達に語りかけるような、ネイティブの口語体であること。

# Pexels検索クエリ生成ルール（映像と台本の1対1連動：素材の被り完全禁止）
- pexels_keyword1、pexels_keyword2、およびpexels_keywordは、それぞれscene1、scene2、および全体の具体的な映像内容と1対1で完全にシンクロ・連動した具体的な英語キーワードにしてください。
- 異なるテーマ（例：「脱走」と「頬袋」など）で同じ背景動画が使い回されるのを完全に防ぐため、台本の内容に合わせてPexelsの検索結果が完全にバラける（自動分散する）ような、超具体的な英語の検索用キーワードを選定してください。
- 一律で「hamster」や「dog」、「cat」などの大雑把な単語のみを検索キーワードにするのは【完全厳禁】です。必ず台本のテーマに深く踏み込んだ2語セット以上の英語フレーズにしてください。
  （選定例）
  - 脱走・ケージ・隙間の話の場合： "hamster escape, hamster cage"
  - 頬袋・食事・ひまわりの種の話の場合： "hamster eating, hamster seeds"
  - 身体能力・走る・夜行性の話の場合： "hamster wheel, hamster running"
  - 単に可愛い・癒やしのエピソードの場合： "cute hamster, small pet"
- 日本の視聴者に響く愛らしくて親近感のある映像にするため、日本の家屋や環境に馴染む要素をブレンドした具体的な英語の検索クエリにしてください。
{f'- ハムスターの映像のみ。犬・猫は厳禁。テーマに合わせ "hamster escape", "hamster wheel", "hamster eating seeds" 等を具体的に指定すること。' if is_hamster else ''}
{f'- 犬の映像のみ。猫・ハムスターは厳禁。日本に馴染む犬種（柴犬、トイプードル等）を優先し、具体的な行動（"shiba inu playing", "toy poodle tilting head" 等）を指定すること。' if is_dog else ''}
{f'- 猫の映像のみ。犬・ハムスターは厳禁。テーマに合わせ具体的なシーン（"cat grooming paw", "kitten sleeping cozy blanket" 等）を指定すること。' if is_cat else ''}

# 出力フォーマット（これ以外のテキストは一切出力禁止）
以下のJSON形式のみを出力してください（scene1とscene2は改行 \\n で区切られた細切れのセクション構造にすること）:
{{"title":"バイラルなタイトル",
"scene1":"前半セクション1\\n前半セクション2\\n前半セクション3\\n前半セクション4",
"scene2":"後半セクション1\\n後半セクション2\\n後半セクション3\\n後半セクション4\\n後半セクション5",
"pexels_keyword1":"scene1と連動した具体的な英語キーワード1",
"pexels_keyword2":"scene2と連動した具体的な英語キーワード2",
"pexels_keyword":"pexels_keyword1とpexels_keyword2をカンマで繋いだもの（例：検索ワード1, 検索ワード2）",
"quiz_answer":"N/A"}}"""

    else:
        # === 英語チャンネル用プロンプト ===
        perspective_instruction = _get_perspective_instruction_en(perspective)
        is_aesthetic = "aesthetic" in profile_key.lower()
        is_dog = "dog" in profile_key.lower()
        is_cat = "pawvana" in profile_key.lower() or "cat" in topic.lower()
        is_lgbtq = "lgbtq" in profile_key.lower()
        is_romance = "romance" in profile_key.lower()
        is_aquatic = "aquatic" in profile_key.lower() or "aquatic" in topic.lower()

        # Pexelsクエリ指示（チャンネルごとの映像スタイル最適化）
        pexels_guidance = ""
        if is_aesthetic:
            pexels_guidance = "- Keywords MUST describe the specific landscape/location mentioned in the script. Use terms like 'aerial cinematic landscape', 'dramatic cliff coastline', etc. NEVER use indoor, person, or animal keywords."
        elif is_dog:
            pexels_guidance = "- Keywords MUST show dogs only. No cats, hamsters, or other animals. Use specific breeds and actions (e.g., 'golden retriever running park', 'german shepherd close up portrait')."
        elif is_cat:
            pexels_guidance = "- Keywords MUST show cats only. No dogs or other animals. Use specific visual contexts (e.g., 'cat grooming close up soft light', 'kitten playing yarn cozy')."
        elif is_lgbtq:
            pexels_guidance = "- Keywords MUST reflect diversity, pride, and stylish concrete visuals. Use terms like 'rainbow flag', 'diverse people celebration', 'pride parade street'. No animals."
        elif is_romance:
            pexels_guidance = "- Keywords MUST reflect emotional, cinematic, romantic atmosphere. Use terms like 'couple silhouette sunset cinematic', 'romantic candlelight close up', 'love letter vintage aesthetic'. No animals."
        elif is_aquatic:
            pexels_guidance = "- Keywords MUST show aquatic or marine life matching the script. Use concrete, visual terms (e.g., 'jellyfish ocean', 'octopus reef', 'coral reef', 'sea turtle'). No dogs, cats, or terrestrial animals."

        prompt = f"""# Your Role and Strict Premise
You are a component of a fully automated script that executes instructions with zero deviation.
Human subjectivity and "typical AI politeness" are completely unnecessary. Follow ONLY the system variables and rules presented.

# Dynamic System Input
- Channel ID: {channel_id}
- Fixed Genre: {genre}
- Fixed Visual Theme: {visual_theme}
- Dynamic Seed: {random_seed}

# Guardrails (Absolute commands to physically prevent AI runaway and duplication)
1. [Genre and Theme Perfect Match]
   All video text MUST be 100% aligned with the genre "{genre}" and the visual theme "{visual_theme}".
   Crossing into off-topic territories is COMPLETELY FORBIDDEN.
   Your focus must be strictly on fascinating aquatic life (marine biology, deep-sea creatures, freshwater marvels, aquarium inhabitants, mysterious ocean anomalies).
2. [First Impression Disposal]
   The "top 3 most obvious/mainstream topics" that come to your mind when you hear "{genre}" have already been used by other channels.
   COMPLETELY DISCARD them. Only adopt the 4th or later, more niche angles.
3. [Seed-Based Forced Diversification]
   Based on the last digit of seed value "{random_seed}":
   {perspective_instruction}

# Violation Penalty
- If you output text unrelated to the genre above, or output commonly repeated/duplicate content,
  the system will treat it as an error and your output will be DISCARDED.

{past_titles_section}

{feedback_section}

# Script Structure (Ultra-Tight 15s Golden Ratio : STRICT 2-SCENE ONLY)
1. [Scene 1 (6-7s)] Short, punchy hook question about a marine/aquatic mystery ending with (?).
2. [Scene 2 (6-7s)] Surprising conclusion explaining the secret, with a comment-triggering ending.

# Strict Rules
- Total word count MUST be 18-22 words max (scene1 + scene2 combined) to ensure it fits within 12-13 seconds of TTS.
- EXACTLY two scenes. No more.
- ZERO emojis or special symbols. They cause rendering corruption.
- Content MUST strictly match the channel genre "{genre}". No off-topic content.
- Must sound like natural native English. No awkward translations.
- Do NOT use brackets like [] or 【】 in the title.

# Pexels Query Rules (Visual-Script 1:1 Sync)
- pexels_keyword1 and pexels_keyword2 MUST match the specific visual context of scene1 and scene2 respectively.
- Generic keywords like "cute puppy" or "sea water" are strictly forbidden. Be specific.
- To ensure successful search hits on Pexels, the use of abstract or conceptual words (e.g., "abstract", "gradient", "contemplation", "diversity") is STRICTLY FORBIDDEN.
- The output search keywords MUST be based on highly visual and concrete entities (e.g., "person", "ocean", "vintage photo", "street", "hug", "clapping").
- Do NOT write long keyword phrases. Strictly limit the keywords to 2 to maximum 3 words, which must be a simple, space-separated combination of concrete nouns and adjectives.
{pexels_guidance}

# Output Format (Output ONLY this JSON, no other text whatsoever)
{{"title":"Viral Title","scene1":"Hook question text?","scene2":"Conclusion text!","pexels_keyword1":"specific scene 1 keyword","pexels_keyword2":"specific scene 2 keyword","quiz_answer":"{'Country name for geography quiz' if is_aesthetic else 'N/A'}"}}"""

    # 最大3回（初回+リトライ2回）
    for attempt in range(1, 4):
        try:
            wait_time = 15 if attempt == 1 else 30
            print(f"[QUOTA_SHIELD] Waiting {wait_time}s before API call (Attempt {attempt}/3)...")
            time.sleep(wait_time)

            # 429リトライ（最大3回、バックオフ増加）
            response = None
            for rate_attempt in range(3):
                try:
                    response = model.generate_content(prompt)
                    break
                except google.api_core.exceptions.ResourceExhausted as rate_e:
                    if rate_attempt < 2:
                        backoff = 30 * (rate_attempt + 1)
                        print(f"[RATE_LIMIT] 429 detected. Backing off {backoff}s... ({rate_attempt+1}/3)")
                        time.sleep(backoff)
                    else:
                        raise rate_e

            text = response.text

            # JSON パース（複数のフォールバック戦略）
            data = _parse_json_response(text)

            title = clean_script_text(data.get("title", f"Insights on {topic}"))
            scene1 = clean_script_text(data.get("scene1", ""))
            scene2 = clean_script_text(data.get("scene2", ""))
            content = f"{scene1}\n{scene2}"

            kw1 = data.get("pexels_keyword1", "nature").strip()
            kw2 = data.get("pexels_keyword2", "nature").strip()
            keyword = f"{kw1},{kw2}"

            quiz_answer = data.get("quiz_answer", None)
            if quiz_answer and quiz_answer.strip().upper() == "N/A":
                quiz_answer = None

            # 物理バリデーション
            if not scene1 or not scene2:
                print(f"[VALIDATION] Empty scene detected. Retry {attempt}/3")
                if attempt < 3:
                    continue

            if language == "ja":
                pure_len = len(content.replace(" ", "").replace("\n", "").replace("\u3000", ""))
                if pure_len > 90 or pure_len < 60:
                    print(f"[VALIDATION] Japanese script {pure_len} chars (must be 60-90). Retry {attempt}/3")
                    if attempt < 3:
                        continue

                # 同一フレーズの重複使用禁止バリデーション
                # scene1とscene2の両方に「知っ」や「知る」が含まれていたらリトライ
                has_shitta_scene1 = any(x in scene1 for x in ["知っ", "知る", "知って"])
                has_shitta_scene2 = any(x in scene2 for x in ["知っ", "知る", "知って"])
                if has_shitta_scene1 and has_shitta_scene2:
                    print(f"[VALIDATION] Duplicate hook/question pattern ('知っ/知る') detected in both scenes. Retry {attempt}/3")
                    if attempt < 3:
                        continue
            else:
                wc = len(content.split())
                if wc > 22 or wc < 16:
                    print(f"[VALIDATION] English script {wc} words (must be 16-22). Retry {attempt}/3")
                    if attempt < 3:
                        continue

            print(f"[GUARD_SYSTEM] Success on attempt {attempt} | SEED={random_seed} | PERSPECTIVE={perspective}")
            return title, content, keyword, quiz_answer

        except (json.JSONDecodeError, ValueError, KeyError) as parse_err:
            print(f"[JSON_PARSE_ERROR] Attempt {attempt}/3: {parse_err}")
            if attempt == 3:
                raise ValueError(f"Failed to parse JSON after 3 attempts: {parse_err}")
        except Exception as e:
            print(f"[GENERATION_ERROR] Attempt {attempt}/3: {e}")
            if attempt == 3:
                raise e

    raise ValueError("Failed to generate script within 3 attempts")


def _parse_json_response(text):
    """JSONレスポンスを複数戦略でパースする"""
    # 前処理：制御文字（改行・タブ・ヌル文字等）や、不要なマークダウン装飾などをクレンジングする
    clean_text = text.strip()
    
    # 1. 制御文字を含むJSONでも許容するように json.loads の strict=False を使用して直接パースを試みる
    try:
        return json.loads(clean_text, strict=False)
    except json.JSONDecodeError:
        pass

    # 2. Markdownコードブロックから抽出
    md_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', clean_text)
    if md_match:
        content = md_match.group(1).strip()
        # 末尾の余分なカンマを削除するなどのクレンジング
        content_cleaned = re.sub(r',\s*([}\]])', r'\1', content)
        try:
            return json.loads(content_cleaned, strict=False)
        except json.JSONDecodeError:
            pass

    # 3. テキスト内のJSONオブジェクト部分を貪欲に検出 ({ から } までの最大範囲)
    obj_match = re.search(r'(\{[\s\S]*\})', clean_text)
    if obj_match:
        content = obj_match.group(1).strip()
        # クレンジング（末尾の不要なカンマの除去）
        content_cleaned = re.sub(r',\s*([}\]])', r'\1', content)
        try:
            return json.loads(content_cleaned, strict=False)
        except json.JSONDecodeError:
            pass

    # 4. 最悪の場合、正規表現でキーと値のペアを抽出しようとする簡易パースフォールバック
    # （JSON形式が完全に崩壊している場合の安全弁）
    fallback_data = {}
    keys = ["title", "scene1", "scene2", "pexels_keyword1", "pexels_keyword2", "quiz_answer"]
    for key in keys:
        # "key": "value" または 'key': 'value' を探す
        key_match = re.search(rf'"{key}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', clean_text)
        if not key_match:
            # シングルクォート対応
            key_match = re.search(rf"'{key}'\s*:\s*'([^'\\]*(?:\\.[^'\\]*)*)'", clean_text)
        if not key_match:
            # クォートのないキーや値の抽出試行
            key_match = re.search(rf'"{key}"\s*:\s*\'([^\'\\]*(?:\\.[^\'\\]*)*)\'', clean_text)
        
        if key_match:
            val = key_match.group(1)
            # エスケープ文字のアンエスケープ
            try:
                val = bytes(val, "utf-8").decode("unicode_escape")
            except Exception:
                pass
            fallback_data[key] = val

    if fallback_data.get("title") or fallback_data.get("scene1"):
        print(f"[WARN] JSON parsed via regex fallback key-value extraction.")
        return fallback_data

    raise ValueError(f"No valid JSON found in response: {text[:300]}")


def clean_script_text(text: str) -> str:
    import re
    if not text:
        return ""
    # 英単語の途中に挟まる不自然なスペースを修復 (例: "secre t" -> "secret", "Wha t's" -> "What's")
    # アスタリスクの周りの不要なスペースを除去し、単語の結合度を高める
    text = re.sub(r'(\b\w+)\s+(t\b|\btell\b)', r'\1\2', text)
    text = re.sub(r'(\bWha\b)\s+(t\'s)', r"What's", text)
    text = re.sub(r'\*\s*([^*]+)\s*\*', r'*\1*', text)
    # 連続した半角スペースを1つに統合
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()
