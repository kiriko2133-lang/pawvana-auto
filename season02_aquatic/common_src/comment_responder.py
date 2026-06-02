"""
GlobeGuess (03_aesthetic_en) コメント自動返信AIシステム
- quiz_answers.json から Video ID と正解データを読み込み
- YouTube Data API で新着コメントを取得
- Gemini で正誤判定＋返信文を生成
- YouTube API で返信を物理的に書き込み
"""
import os
import sys
import json
import re
import google.generativeai as genai
from googleapiclient.errors import HttpError

def save_quiz_answer(work_dir, video_id, title, answer, script_content):
    """
    アップロード成功時に呼び出し、Video IDと正解データをquiz_answers.jsonに追記。
    """
    answers_path = os.path.join(work_dir, "quiz_answers.json")
    data = {}
    if os.path.exists(answers_path):
        try:
            with open(answers_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except:
            data = {}
    
    data[video_id] = {
        "title": title,
        "answer": answer,
        "script": script_content
    }
    
    with open(answers_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"[QUIZ] Saved answer for {video_id}: {answer}")


def check_and_respond_comments(profile_key, work_dir=".", youtube_service=None, gemini_key=None):
    """
    全チャンネル共通: チャンネルに紐づく新着コメントを一括取得し、未返信のものにAI自動返信する。
    quiz_answers.json があればクイズの正誤判定を行い、無ければ汎用的なファンサ返信を行う。
    """
    if gemini_key:
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
            genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel('gemini-2.0-flash')
    
    total_replies = 0
    
    # クイズデータの読み込み（存在する場合のみ）
    answers_path = os.path.join(work_dir, "quiz_answers.json")
    quiz_data = {}
    if os.path.exists(answers_path):
        with open(answers_path, 'r', encoding='utf-8') as f:
            quiz_data = json.load(f)
            
    try:
        # 自チャンネルのIDを取得
        channel_res = youtube_service.channels().list(part="id", mine=True).execute()
        if not channel_res.get("items"):
            print(f"[COMMENT_AI] Could not get channel ID for {profile_key}")
            return
        channel_id = channel_res["items"][0]["id"]
        
        # チャンネルに関連するすべての新着コメントスレッドを取得 (最大50件)
        response = youtube_service.commentThreads().list(
            part="snippet",
            allThreadsRelatedToChannelId=channel_id,
            maxResults=50,
            order="time"
        ).execute()
        
        threads = response.get("items", [])
        if not threads:
            print(f"[COMMENT_AI] No new comments found for {profile_key}.")
            return
            
        for thread in threads:
            snippet = thread["snippet"]
            top_comment = snippet["topLevelComment"]["snippet"]
            video_id = top_comment.get("videoId")
            comment_text = top_comment["textDisplay"]
            comment_author = top_comment["authorDisplayName"]
            comment_id = top_comment["id"]
            reply_count = snippet.get("totalReplyCount", 0)
            
            # 既に返信済みのコメントはスキップ
            if reply_count > 0:
                continue
            
            # 自分自身のコメントにはスキップ
            if top_comment.get("authorChannelId", {}).get("value") == channel_id:
                continue
            
            print(f"\n  [NEW] @{comment_author} on Video {video_id}: \"{comment_text}\"")
            
            # クイズ正解データがある場合はクイズ用プロンプト、無い場合は汎用プロンプト
            q_info = quiz_data.get(video_id, {})
            correct_answer = q_info.get("answer", "")
            video_title = q_info.get("title", "")
            
            if correct_answer:
                judge_prompt = f"""
You are the friendly host of a YouTube channel.
A viewer commented on a video titled "{video_title}".
The correct answer for this video's quiz is: "{correct_answer}"
The viewer's comment is: "{comment_text}"

Your task:
1. Determine if the viewer's guess is CORRECT, CLOSE (partially right), or WRONG.
2. Write a short, enthusiastic reply in English (MAX 2 sentences).
   - If CORRECT: Congratulate them warmly and add a fun bonus fact.
   - If CLOSE: Acknowledge they're close and give a gentle hint.
   - If WRONG: Encourage them kindly and tease the answer without revealing it.
   - If NOT A GUESS: Reply warmly and encourage them to guess.
3. NEVER use emojis. Keep it natural and engaging.

Reply ONLY with the reply text.
"""
            else:
                judge_prompt = f"""
You are the friendly host of a YouTube channel related to {profile_key}.
A viewer commented on your video.
The viewer's comment is: "{comment_text}"

Your task:
Write a short, enthusiastic reply in English or Japanese (match the language of their comment) (MAX 2 sentences).
NEVER use emojis. Keep it natural, engaging, and appreciative.

Reply ONLY with the reply text.
"""
            
            try:
                ai_response = model.generate_content(judge_prompt)
                reply_text = ai_response.text.strip()
                
                # 安全チェック: 空文字や異常に長い返信を防止
                if not reply_text or len(reply_text) > 500:
                    print(f"  [SKIP] AI reply invalid (len={len(reply_text) if reply_text else 0})")
                    continue
                
                print(f"  [REPLY] -> \"{reply_text}\"")
                
                # YouTube APIで返信を物理的に書き込み
                youtube_service.comments().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "parentId": comment_id,
                            "textOriginal": reply_text
                        }
                    }
                ).execute()
                
                print(f"  [POSTED] Reply posted to @{comment_author}")
                total_replies += 1
                
            except HttpError as he:
                print(f"  [API_ERROR] {he}")
            except Exception as ai_err:
                print(f"  [AI_ERROR] {ai_err}")
                
    except HttpError as e:
        if e.resp.status == 403:
            print(f"[COMMENT_AI] Comments disabled or forbidden for {profile_key}.")
        else:
            print(f"[COMMENT_AI] API error for {profile_key}: {e}")
    except Exception as e:
        print(f"[COMMENT_AI] Error processing {profile_key}: {e}")

    print(f"\n[COMMENT_AI] Total replies posted for {profile_key}: {total_replies}")


if __name__ == "__main__":
    """
    単体実行: python common_src/comment_responder.py 03_aesthetic_en
    全巡回: python common_src/comment_responder.py ALL
    """
    sys.path.append(os.path.dirname(__file__))
    from main import get_authenticated_service, SCOPES_YOUTUBE, load_config
    
    arg = sys.argv[1] if len(sys.argv) > 1 else "03_aesthetic_en"
    
    channels_to_process = []
    if arg == "ALL":
        # リスク回避のため、議論・対話空間である「07_lgbtq_en」および「08_romance_en」を完全に除外リストに指定
        channels_to_process = [
            "01_dogs_jp", "02_pets_jp", "03_aesthetic_en", "04_pawvana",
            "05_ham_jp", "06_dogs_en"
        ]
    else:
        channels_to_process = [arg]
        
    for work_dir in channels_to_process:
        # 二重の安全弁：引数で直接個別指定された場合でも、07（LGBTQ）または08（Romance）なら強制スキップして終了させる
        if work_dir in ["07_lgbtq_en", "08_romance_en"]:
            print(f"[SECURITY_BYPASS] Channel {work_dir} is strictly excluded from AI responding to preserve community debate.")
            continue
            
        print(f"\n{'='*50}\n[START] Processing channel: {work_dir}\n{'='*50}")
        config_path = os.path.join(work_dir, "config.json")
        if not os.path.exists(config_path):
            print(f"[SKIP] config.json not found in {work_dir}")
            continue
            
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        profile_key = list(config.keys())[0]
        p = config[profile_key]
        
        token_path = os.path.join(work_dir, "tokens", "youtube.pickle")
        try:
            youtube = get_authenticated_service(
                'youtube', 'v3', SCOPES_YOUTUBE,
                token_path=token_path,
                profile_key=profile_key,
                work_dir=work_dir
            )
        except Exception as e:
            print(f"[AUTH_ERROR] Could not authenticate for {work_dir}: {e}")
            continue
        
        gemini_key = (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get(f"GEMINI_API_KEY_{profile_key.upper()}")
            or p.get('gemini_api_key')
        )
        if gemini_key == "REDACTED_API_KEY":
            gemini_key = None
            
        check_and_respond_comments(
            profile_key=profile_key,
            work_dir=work_dir,
            youtube_service=youtube,
            gemini_key=gemini_key
        )
