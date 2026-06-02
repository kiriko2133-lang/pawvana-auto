import os
import sys
import asyncio
import random
import json
import datetime
import re
import time
import traceback
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# Windowsでの文字化け・エンコードエラー対策
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from main import get_authenticated_service, check_youtube_channel, upload_to_youtube, SCOPES_TASKS, SCOPES_YOUTUBE, load_config
import ai_generator
import generate_video

# auditor.py による事後監査APIコールは廃止済み
# 品質監査ルールは ai_generator.py のプロンプトに統合済み

def cleanse_japanese_text(text):
    if not text:
        return ""
    # すべての半角スペースと全角スペースを物理的に完全消去
    text = text.replace(" ", "").replace("　", "")
    
    # 典型的な絵文字削除跡地や助詞の乱れの補正
    # 1. 「人はで教えてね」 -> 「人はコメントで教えてね」
    text = re.sub(r"人はで教えて", "人はコメントで教えて", text)
    text = re.sub(r"人はでコメント", "人はコメント", text)
    
    # 2. 「人はで」 -> 「人は」
    text = re.sub(r"人はで([、。！!?]|$)", r"人は\1", text)
    # 一般的な「はで」の補正
    text = re.sub(r"(\w+)はで(教えて|書いて|コメント|反応)", r"\1はコメントで\2", text)
    text = re.sub(r"(\w+)はで(？！|？！|？|！|\?|\!)", r"\1は\2", text)
    
    # 3. 文頭や読点後の不要な「で」の補正
    text = re.sub(r"([。！!？\?\n])で教えて", r"\1コメントで教えて", text)
    
    # 4. 重複しやすい表現の整理
    text = re.sub(r"コメント欄でコメントで", "コメント欄で", text)
    text = re.sub(r"コメントでコメントで", "コメントで", text)
    
    return text

def strip_emojis(text, is_ja_channel=False):
    """
    動画合成時の文字化け(□)を防ぐため、絵文字や特殊記号を徹底的に除去する。
    日本語文字は維持する。
    is_ja_channel が True の場合、スペースに置換せず完全に削除し、最終的なクレンジングも行う。
    """
    # ASCII + 日本語（ひらがな、カタカナ、漢字、句読点）
    pattern = r'[^\x00-\x7F\u3000-\u303F\u3040-\u309F\u30A0-\u30FF\uFF00-\uFFEF\u4E00-\u9FAF\n]+'
    if is_ja_channel:
        cleaned = re.sub(pattern, '', text)
        return cleanse_japanese_text(cleaned)
    else:
        return re.sub(pattern, ' ', text).strip()

async def run_auto_post(work_dir=".", topic=None):
    """
    15秒構成のシンプルな自動投稿プロセス。
    AI監査員とのループ機能を搭載。
    """
    # 1. 認証と設定の読み込み
    config_path = os.path.join(work_dir, 'config.json')
    if not os.path.exists(config_path):
        print(f"FATAL: config not found: {config_path}")
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    # Load history log for duplicate detection
    history_path = os.path.join(work_dir, "generated_history.json")
    if os.path.exists(history_path):
        try:
            with open(history_path, "r", encoding="utf-8") as f_hist:
                history_log = json.load(f_hist)
        except Exception:
            history_log = []
    else:
        history_log = []
    history_log = history_log[-20:] # 直近20件に制限
    
    config_profile_key = list(config.keys())[0]
    p = config[config_profile_key]
    
    # profile_key をフォルダ名形式に正規化（ai_generator の CHANNEL_METADATA 解決に使用）
    # work_dir のベースネーム（例: "01_dogs_jp"）を優先的に使用する
    folder_name = os.path.basename(os.path.abspath(work_dir))
    # フォルダ名が数字プレフィックス付き（01_dogs_jp 等）ならそれを使用、
    # そうでなければ config 内のキーをそのまま使用
    if any(folder_name.startswith(f"{i:02d}_") for i in range(1, 20)):
        profile_key = folder_name
    else:
        profile_key = config_profile_key
    
    print(f"PROFILE: {profile_key} (config_key: {config_profile_key})")
    print(f"CHANNEL_ID: {p['channel_id']}")
    print(f"PROFILE_NAME: {p['profile_name']}")
    
    # チャンネル固有のテーマ設定
    if not topic:
        if "topics" in p and p["topics"]:
            topics = p["topics"]
        elif "en" in profile_key.lower():
            if "aesthetic" in profile_key.lower():
                topics = ["Stunning hidden gems", "Visually shocking landscapes", "Cinematic global paradise", "Mysterious geography secrets", "Breathtaking world wonders"]
            elif "dog" in profile_key.lower():
                topics = ["Funny dog facts", "Puppy joy", "Dog training tips", "Smart dog tricks", "Living with dogs"]
            elif "aquatic" in profile_key.lower():
                topics = ["Deep sea mysteries", "Strange ocean creatures", "Coral reef secrets", "Freshwater wonders", "Aquarium life hacks"]
            else:
                topics = ["Cute animal moments", "Animal facts", "Heartwarming pets"]
        elif "dog" in profile_key.lower():
            topics = ["犬の豆知識", "子犬の癒やし", "犬のしつけ", "賢い犬の行動", "犬との暮らし"]
        elif "pet" in profile_key.lower():
            topics = ["猫の豆知識", "子猫の癒やし", "猫の不思議な行動", "猫との暮らし"]
        elif "ham" in profile_key.lower():
            topics = ["ハムスターの豆知識", "ハムスターの癒やし", "ハムスターの不思議な行動", "ハムスターとの暮らし"]
        else:
            topics = ["動物の豆知識", "ペットの不思議な行動", "癒やしの動物映像"]
        topic = random.choice(topics)
        
    # 言語の判定（profile_key またはフォルダ名に jp が含まれる場合は確実に ja と判定し、英語チャンネルと100%隔離）
    language = "ja" if "jp" in profile_key.lower() or "jp" in folder_name.lower() else "en"
    
    print(f"=== AUTO POST START: {p['profile_name']} (topic: {topic}, lang: {language}) ===")
    
    try:
        # 1. 認証
        youtube_token = os.path.join(work_dir, "tokens", "youtube.pickle")
        env_token_key = f"YOUTUBE_TOKEN_{profile_key.upper()}_B64"
        os.makedirs(os.path.join(work_dir, "tokens"), exist_ok=True)
        
        youtube_service = get_authenticated_service(
            'youtube', 'v3', SCOPES_YOUTUBE, 
            token_path=youtube_token, 
            env_token_key=env_token_key, 
            profile_key=profile_key,
            work_dir=work_dir
        )
        
        expected_channel_id = p['channel_id']
        if not check_youtube_channel(youtube_service, expected_channel_id):
            raise Exception(f"Channel ID mismatch for {expected_channel_id}")

        # 2. 台本生成（AIエージェント・ループ）
        print("STEP: Script generation starting...")
        
        # APIキー取得（フォールバック付き）
        gemini_key = (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get(f"GEMINI_API_KEY_{profile_key.upper()}")
            or p.get('gemini_api_key')
        )
        if gemini_key == "REDACTED_API_KEY":
            gemini_key = None
        print(f"GEMINI_KEY exists: {bool(gemini_key)}")
        if not gemini_key:
            raise Exception("FATAL: No Gemini API key available")
        
        title, script_content, search_query, quiz_answer = "", "", "", None
        current_feedback = None
        max_attempts = 3
        final_valid_content = False
        
        # チャンネルの文脈（forbidden_animals のみ。ジャンルと世界観はガードシステムが自動注入）
        target_animal = p.get('target_animal', 'pets')
        forbidden = ", ".join(p.get('forbidden_animals', []))
        channel_context = f"DO NOT mention: {forbidden}." if forbidden else ""
        
        print(f"[GUARD_SYSTEM] Profile key for metadata resolution: {profile_key}")
        
        # Gemini APIへ渡す過去タイトル一覧
        past_titles = [entry.get("title", "") for entry in history_log if entry.get("title")]
        
        for attempt in range(1, max_attempts + 1):
            print(f"ATTEMPT {attempt}/{max_attempts}: Generating script (GUARD SYSTEM v2)...")
            title, script_content, search_query, quiz_answer = ai_generator.generate_viral_script(
                topic, channel_context=channel_context, api_key=gemini_key, feedback=current_feedback, language=language, profile_key=profile_key, past_titles=past_titles
            )
            
            # 英語チャンネル（日本語以外）限定のアポストロフィ保護と結合
            if language != "ja":
                title = re.sub(r"[’‘´`]", "'", title)
                script_content = re.sub(r"[’‘´`]", "'", script_content)

            # 文字化け対策
            is_ja_channel = (language == "ja")
            title = strip_emojis(title, is_ja_channel=is_ja_channel)
            script_content = strip_emojis(script_content, is_ja_channel=is_ja_channel)
            
            if is_ja_channel:
                title = cleanse_japanese_text(title)
                script_content = cleanse_japanese_text(script_content)

            # 英語チャンネル（日本語以外）限定の最終クレンジング
            if language != "ja":
                title = re.sub(r"(\w+)\s*'\s*(s|t|re|ve|ll|m|d)\b", r"\1'\2", title, flags=re.IGNORECASE)
                title = re.sub(r"\bIt\s+s\b", "It's", title, flags=re.IGNORECASE)
                script_content = re.sub(r"(\w+)\s*'\s*(s|t|re|ve|ll|m|d)\b", r"\1'\2", script_content, flags=re.IGNORECASE)
                script_content = re.sub(r"\bIt\s+s\b", "It's", script_content, flags=re.IGNORECASE)
            
            print(f"TITLE: {title}")
            print(f"SCRIPT: {script_content[:100]}...")
            print(f"SEARCH_QUERY: {search_query}")

            # 重複テーマ・台本内容のチェック
            duplicate_found = False
            from difflib import SequenceMatcher
            def clean_text_for_sim(t):
                return re.sub(r'[\s　、。！？!?\-_]', '', t).lower()
                
            cleaned_new_title = clean_text_for_sim(title)
            cleaned_new_script = clean_text_for_sim(script_content)
            
            for entry in history_log:
                prev_title = entry.get("title", "")
                prev_script = entry.get("script_content", "")
                
                cleaned_prev_title = clean_text_for_sim(prev_title)
                cleaned_prev_script = clean_text_for_sim(prev_script)
                
                # 完全一致
                if cleaned_new_title == cleaned_prev_title or cleaned_new_script == cleaned_prev_script:
                    duplicate_found = True
                    print(f"[DUPLICATE] Exact match found with past entry. Title: '{prev_title}'")
                    break
                    
                # 類似度
                title_ratio = SequenceMatcher(None, cleaned_new_title, cleaned_prev_title).ratio()
                script_ratio = SequenceMatcher(None, cleaned_new_script, cleaned_prev_script).ratio()
                if title_ratio > 0.7 or script_ratio > 0.7:
                    duplicate_found = True
                    print(f"[DUPLICATE] High similarity detected. Title ratio: {title_ratio:.2f}, Script ratio: {script_ratio:.2f}")
                    break
                    
                # キーワード部分一致
                match = SequenceMatcher(None, cleaned_new_title, cleaned_prev_title).find_longest_match(0, len(cleaned_new_title), 0, len(cleaned_prev_title))
                if match.size >= 3:
                    common_str = cleaned_new_title[match.a : match.a + match.size]
                    ignore_patterns = ["について", "のひみつ", "の秘密", "雑学", "あるある", "の生態", "驚きの", "不思議", "なぜ", "どうして", "とは"]
                    is_ignored = False
                    for pat in ignore_patterns:
                        if common_str in pat or pat in common_str:
                            is_ignored = True
                            break
                    if not is_ignored:
                        duplicate_found = True
                        print(f"[DUPLICATE] Common keyword '{common_str}' (len: {match.size}) found in past title '{prev_title}'.")
                        break
            
            if duplicate_found:
                current_feedback = f"前回の出力 '{title}' は過去のコンテンツとテーマや内容が重複しています。完全に異なる新しいトピック・テーマで台本を生成してください。"
                
                # topic を再選定する
                if "topics" in p and p["topics"]:
                    other_topics = [t for t in p["topics"] if t != topic]
                    if other_topics:
                        topic = random.choice(other_topics)
                    else:
                        topic = random.choice(p["topics"])
                else:
                    if "en" in profile_key.lower():
                        if "aesthetic" in profile_key.lower():
                            default_topics = ["Stunning hidden gems", "Visually shocking landscapes", "Cinematic global paradise", "Mysterious geography secrets", "Breathtaking world wonders"]
                        elif "dog" in profile_key.lower():
                            default_topics = ["Funny dog facts", "Puppy joy", "Dog training tips", "Smart dog tricks", "Living with dogs"]
                        else:
                            default_topics = ["Cute animal moments", "Animal facts", "Heartwarming pets"]
                    elif "dog" in profile_key.lower():
                        default_topics = ["犬の豆知識", "子犬の癒やし", "犬のしつけ", "賢い犬の行動", "犬との暮らし"]
                    elif "pet" in profile_key.lower():
                        default_topics = ["猫の豆知識", "子猫の癒やし", "猫の不思議な行動", "猫との暮らし"]
                    elif "ham" in profile_key.lower():
                        default_topics = ["ハムスターの豆知識", "ハムスターの癒やし", "ハムスターの不思議な行動", "ハムスターとの暮らし"]
                    else:
                        default_topics = ["動物の豆知識", "ペットの不思議な行動", "癒やしの動物映像"]
                    other_topics = [t for t in default_topics if t != topic]
                    if other_topics:
                        topic = random.choice(other_topics)
                    else:
                        topic = random.choice(default_topics)
                
                print(f"[DUPLICATE] Retrying script generation with new topic: '{topic}'")
                continue
            print("STEP: Audio duration check...")
            temp_audio_path = os.path.join(work_dir, "temp_audio_check.mp3")
            try:
                await generate_video.generate_speech(script_content, temp_audio_path, voice=p['voice'], rate="+15%")
                from moviepy.editor import AudioFileClip
                a_clip = AudioFileClip(temp_audio_path)
                audio_dur = a_clip.duration
                a_clip.close()
                print(f"AUDIO_DURATION: {audio_dur:.2f}s")
                
                if audio_dur > 15.0:
                    current_feedback = f"THE SCRIPT IS TOO LONG ({audio_dur:.1f}s). Please shorten it to be under 14 seconds. Current text: {script_content}"
                    print(f"FAIL: Too long ({audio_dur:.1f}s). Retrying.")
                    continue
                
                # 監査はプロンプト内で完結済み。音声長OKなら合格。
                print("[ONE_CALL_JSON] Script validated (auditor integrated in prompt).")
                final_valid_content = True
                break
            except Exception as e:
                print(f"DURATION_CHECK_ERROR: {e}")
                traceback.print_exc()
                current_feedback = "Error checking duration. Please try again with a simpler, shorter script."

        if not final_valid_content:
            print("FATAL: Could not generate valid script in max attempts.")
            sys.exit(1)

        # 3. 素材取得（Pexels API + ローカルテーマ動画 fallback）
        print(f"STEP: Fetching ambient visual...")
        pexels_key = (
            os.environ.get("PEXELS_API_KEY")
            or os.environ.get(f"PEXELS_API_KEY_{profile_key.upper()}")
            or p.get('pexels_api_key')
        )
        if pexels_key == "REDACTED_API_KEY":
            pexels_key = ""
            
        try:
            asset_path, asset_type = await generate_video.fetch_best_visual(
                search_query, pexels_key, profile_key=profile_key, work_dir=work_dir
            )
            print(f"ASSET: path={asset_path}, type={asset_type}")
            if not asset_path and asset_type != "color":
                raise Exception("No visual asset found.")
        except Exception as visual_err:
            print(f"[WARN] Failed to fetch visual assets (API & local fallback failed): {visual_err}")
            print("[SAFE_SKIP] Skipping video generation and posting for this run to prevent system crash.")
            sys.exit(0)
        
        # 4. 動画合成
        print("STEP: Video assembly (15s)...")
        video_output_path = os.path.join(work_dir, "youtube_short.mp4")
        
        # BGMの存在確認とフォールバック
        bgm_path = p.get('bgm', 'bgm.mp3')
        if not os.path.exists(bgm_path):
            work_bgm = os.path.join(work_dir, bgm_path)
            if os.path.exists(work_bgm):
                bgm_path = work_bgm
            elif os.path.exists("bgm.mp3"):
                print(f"BGM_FALLBACK: Using root bgm.mp3")
                bgm_path = "bgm.mp3"
            else:
                print(f"BGM_MISSING: No BGM file found")
                bgm_path = None

        video_file, success = await generate_video.assemble_video_professional(
            script_content, 
            asset_path,
            asset_type,
            bgm_path, 
            video_output_path,
            voice=p['voice'],
            topic=profile_key,
            work_dir=work_dir
        )
        
        if not success or not video_file:
            raise Exception("FATAL: Video generation failed")
        
        if not os.path.exists(video_file):
            raise Exception(f"FATAL: Video file does not exist: {video_file}")
        
        video_size = os.path.getsize(video_file)
        print(f"VIDEO_FILE: {video_file} ({video_size} bytes)")
        
        if video_size < 1000:
            raise Exception(f"FATAL: Video file too small: {video_size} bytes")
        # Save successful title, script content, and background video IDs to history log
        video_ids = []
        if asset_path:
            for part in asset_path.split(','):
                match = re.search(r'temp_bg_\d+_(\d+)\.mp4', part)
                if match:
                    video_ids.append(int(match.group(1)))
                    
        history_log.append({
            "title": title,
            "script_content": script_content,
            "video_ids": video_ids,
            "timestamp": datetime.datetime.now().isoformat()
        })
        history_log = history_log[-20:] # 直近20件に制限
        
        with open(history_path, "w", encoding="utf-8") as f_hist:
            json.dump(history_log, f_hist, ensure_ascii=False, indent=2)
    

        # 4.1 二重投稿ガード（Time-Lock Guard）
        print("STEP: Double-posting time-lock check...")
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            # チャンネルの最新動画を取得
            search_res = youtube_service.search().list(
                channelId=expected_channel_id,
                order="date",
                part="snippet",
                type="video",
                maxResults=1
            ).execute()
            
            items = search_res.get("items", [])
            if items:
                latest_video = items[0]
                pub_time_str = latest_video["snippet"]["publishedAt"]
                # フォーマット例: 2026-05-18T12:00:00Z
                pub_time = datetime.datetime.strptime(pub_time_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                diff = now_utc - pub_time
                diff_minutes = diff.total_seconds() / 60.0
                print(f"LATEST_POST_TIME: {pub_time_str} (Diff: {diff_minutes:.1f} minutes ago)")
                if diff_minutes < 10.0:
                    print(f"[WARN] A video was posted {diff_minutes:.1f} minutes ago. Aborting this run to prevent double-posting.")
                    sys.exit(0)  # エラーではなく、正常終了として終了させる（ActionsのステータスはGreenにする）
            else:
                print("No previous videos found. Proceeding safely.")
        except Exception as guard_err:
            print(f"[WARN] Failed to query latest video for double-post check (proceeding safely): {guard_err}")

        # 5. YouTubeアップロード + 検証
        print("STEP: YouTube upload starting...")
        print(f"UPLOAD_TITLE: {title}")
        print(f"UPLOAD_CHANNEL: {expected_channel_id}")
        
        full_description = f"{script_content}\n\n{p['tags']}"
            
        body = {
            'snippet': {
                'title': title,
                'description': full_description,
                'tags': ['Shorts'] + p['tags'].replace('#', '').split(),
                'categoryId': '22'
            },
            'status': {
                'privacyStatus': 'public',
                'selfDeclaredMadeForKids': False
            }
        }
        
        # === ハッシュタグの強制挿入 ===
        if "snippet" in body and "description" in body["snippet"] and body["snippet"]["description"] is not None:
            if profile_key == "01_dogs_jp":
                body["snippet"]["description"] += "\n\n#shorts #chihuahua #dog #子犬 #犬の楽園"
            elif profile_key == "02_beauty_en" or profile_key == "glow_haven" or "beauty" in profile_key.lower():
                body["snippet"]["description"] += "\n\n#shorts #wellness #lifestyle #beauty #health"
        
        media = MediaFileUpload(video_file, chunksize=-1, resumable=True, mimetype='video/mp4')
        request = youtube_service.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"UPLOAD_PROGRESS: {int(status.progress() * 100)}%")
        
        print(f"UPLOAD_RESPONSE: {json.dumps(response, ensure_ascii=False, default=str)}")
        
        video_id = response.get("id")
        if not video_id:
            print(f"UPLOAD_RESPONSE_FULL: {response}")
            raise Exception("FATAL: video_id missing from upload response")
        
        print(f"VIDEO_ID: {video_id}")
        
        # 6. アップロード検証 - videos().list() で実在確認
        print("STEP: Upload verification...")
        import time
        time.sleep(3)  # YouTube側の処理待ち
        
        verify = youtube_service.videos().list(
            part="status,snippet",
            id=video_id
        ).execute()
        
        print(f"VERIFY_RESPONSE: {json.dumps(verify, ensure_ascii=False, default=str)}")
        
        items = verify.get("items", [])
        if not items:
            raise Exception(f"FATAL: Uploaded video {video_id} not found via videos().list()")
        
        video = items[0]
        actual_channel = video["snippet"]["channelId"]
        upload_status = video["status"].get("uploadStatus")
        
        print(f"VERIFY_CHANNEL: {actual_channel}")
        print(f"VERIFY_UPLOAD_STATUS: {upload_status}")
        
        if actual_channel != expected_channel_id:
            raise Exception(f"FATAL: Channel mismatch. Expected={expected_channel_id}, Actual={actual_channel}")
        
        if upload_status not in ("uploaded", "processed"):
            raise Exception(f"FATAL: Upload status invalid: {upload_status}")
        
        print(f"UPLOAD_SUCCESS: {video_id}")
        print(f"URL: https://www.youtube.com/shorts/{video_id}")
        
        # GlobeGuess (aesthetic) チャンネルの場合、クイズ正解データを保存
        if "aesthetic" in profile_key and quiz_answer:
            try:
                import comment_responder
                comment_responder.save_quiz_answer(work_dir, video_id, title, quiz_answer, script_content)
            except Exception as qe:
                print(f"[QUIZ_SAVE_WARN] {qe}")
        
        # コメント自動返信（aesthetic チャンネルのみ）
        if "aesthetic" in profile_key:
            try:
                import comment_responder
                comment_responder.check_and_respond_comments(
                    profile_key=profile_key,
                    work_dir=work_dir,
                    youtube_service=youtube_service,
                    gemini_key=gemini_key
                )
            except Exception as cr_err:
                print(f"[COMMENT_RESPONDER_WARN] {cr_err}")

    except HttpError as e:
        print(f"HTTP_ERROR: status={e.resp.status}")
        print(f"HTTP_ERROR_CONTENT: {e.content.decode('utf-8') if hasattr(e, 'content') else str(e)}")
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"FATAL_ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        print("[CLEANUP] Running final proactive workspace cleanup in finally clause...")
        import glob
        import shutil
        
        # 1. 一時ディレクトリ 'temp_audio' の削除
        temp_audio_dir = os.path.join(work_dir, "temp_audio")
        if os.path.exists(temp_audio_dir):
            try:
                shutil.rmtree(temp_audio_dir)
                print(f"[CLEANUP] Removed directory: {temp_audio_dir}")
            except Exception as clean_err:
                print(f"[CLEANUP_WARN] Failed to remove {temp_audio_dir}: {clean_err}")
                
        # 2. 個別の一時ファイル削除
        temp_patterns = [
            "temp_audio_check.mp3",
            "temp_bg_*.mp4",
            "youtube_short.mp4",
            "temp_video_noaudio_*.mp4",
            "temp_final_audio_*.wav",
            "*.png",
            "*.mp3"
        ]
        
        for pattern in temp_patterns:
            files = glob.glob(os.path.join(work_dir, pattern))
            for f in files:
                filename = os.path.basename(f)
                # ソースコードや設定ファイル、および元のBGMファイルは絶対に削除しない
                if filename.endswith(".py") or filename == "config.json" or filename == "generated_history.json" or filename == "bgm.mp3":
                    continue
                try:
                    os.remove(f)
                    print(f"[CLEANUP] Removed temporary file: {f}")
                except Exception as clean_err:
                    print(f"[CLEANUP_WARN] Failed to remove file {f}: {clean_err}")

if __name__ == "__main__":
    w_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    t_key = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(run_auto_post(w_dir, t_key))
