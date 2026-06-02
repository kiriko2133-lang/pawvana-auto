import os
import pickle
import json
import sys
import base64
import asyncio
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
import generate_video

# OAuthのスコープ変更エラーを回避する設定
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

# 権限スコープの定義
SCOPES_TASKS = [
    'https://www.googleapis.com/auth/tasks.readonly',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]
SCOPES_YOUTUBE = [
    'https://www.googleapis.com/auth/youtube'
]

def load_config(work_dir="."):
    """config.jsonから設定を読み込む"""
    config_path = os.path.join(work_dir, 'config.json')
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_authenticated_service(api_name, api_version, scopes, token_path=None, env_token_key=None, profile_key=None, work_dir="."):
    """汎用的な認証・サービス取得関数 (ローカルファイル & 環境変数の両方に対応)"""
    creds = None
    
    # 1. 環境変数からのBase64トークン読み込み（GlobeGuess等のPickle方式）
    if env_token_key and os.environ.get(env_token_key):
        try:
            print(f"[DEBUG] 環境変数 {env_token_key} からBase64トークンを復元します。")
            token_data = base64.b64decode(os.environ.get(env_token_key))
            creds = pickle.loads(token_data)
            print("[INFO] Base64トークンの復元に成功しました。")
        except Exception as e:
            print(f"[ERROR] Base64トークン復元失敗: {e}")

    # 2. 個別環境変数からの認証（dogs_jp, hamsters_jp, pets, pets_jp用）
    if not creds and profile_key:
        client_id = os.environ.get(f"YOUTUBE_CLIENT_ID_{profile_key.upper()}") or os.environ.get("YOUTUBE_CLIENT_ID")
        client_secret = os.environ.get(f"YOUTUBE_CLIENT_SECRET_{profile_key.upper()}") or os.environ.get("YOUTUBE_CLIENT_SECRET")
        refresh_token = os.environ.get(f"YOUTUBE_REFRESH_TOKEN_{profile_key.upper()}")
        
        print(f"AUTH_PROFILE: {profile_key}")
        print(f"CLIENT_ID exists: {bool(client_id)}")
        print(f"CLIENT_SECRET exists: {bool(client_secret)}")
        print(f"REFRESH_TOKEN exists: {bool(refresh_token)}")
        
        if all([client_id, client_secret, refresh_token]):
            print(f"AUTH: Building credentials from env vars (profile: {profile_key})")
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=scopes
            )
            print("AUTH: Credentials object created successfully.")
        else:
            print(f"AUTH_WARN: Missing env vars for profile {profile_key}")

    # 3. ローカルファイルからの読み込み
    if not creds and token_path and os.path.exists(token_path):
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)
            print("[INFO] ローカルのトークンファイルを読み込みました。")
            
    # 認証情報の自動更新（ローカル & クラウド共通）
    if creds:
        print(f"AUTH_CREDS_VALID: {creds.valid}")
        print(f"AUTH_CREDS_EXPIRED: {creds.expired}")
        print(f"AUTH_TOKEN_URI: {creds.token_uri}")
        print(f"AUTH_HAS_REFRESH: {bool(creds.refresh_token)}")
    
    if creds and (not creds.valid or creds.expired) and creds.refresh_token:
        try:
            creds.refresh(Request())
            print("AUTH: Token refreshed successfully.")
            print(f"AUTH_CREDS_VALID_AFTER: {creds.valid}")
        except Exception as e:
            print(f"AUTH_REFRESH_ERROR: {e}")
            import traceback
            traceback.print_exc()
            creds = None

    # 新規認証（ローカル環境専用。上記すべてで認証が取れなかった場合）
    if not creds or not creds.valid:
        # GitHub Actions環境ではブラウザ認証ができないため、エラーを出して終了させる
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("\n[ERROR] 🚨 認証トークンが無効または期限切れです。")
            print("ローカル環境で get_refresh_token_...py を実行し、生成された新しいトークンを")
            print("GitHubのSecretsに登録し直してください。")
            raise Exception("Authentication token is invalid or expired in GitHub Actions environment.")

        if token_path:
            os.makedirs(os.path.dirname(token_path), exist_ok=True) if os.path.dirname(token_path) else None
        print(f"\n--- {api_name} の新規認証を開始します ---")
        
        # 利用可能な認証ファイルを順番に試す
        client_secret_candidates = [
            os.path.join(work_dir, 'credentials.json'),
            os.path.join(work_dir, 'credentials_2.json'),
            os.path.join(work_dir, 'credentials_3.json'),
            'credentials.json' # フォールバック
        ]
        
        flow = None
        for secret_file in client_secret_candidates:
            if os.path.exists(secret_file):
                try:
                    print(f"[INFO] 認証ファイル {secret_file} を使用して認証を試みます...")
                    flow = InstalledAppFlow.from_client_secrets_file(secret_file, scopes)
                    break
                except Exception as e:
                    print(f"[WARN] {secret_file} でのエラー: {e}")
        
        if not flow:
            print("[ERROR] 利用可能な credentials*.json が一つも見つかりません。")
            raise Exception("No credentials file available.")
             
        creds = flow.run_local_server(port=0, prompt='consent select_account')
        
        if token_path:
            with open(token_path, 'wb') as token:
                pickle.dump(creds, token)
        print("[INFO] 新規認証が完了し、トークンを保存しました。")
            
    print(f"[INFO] YouTube API ({api_name} {api_version}) のビルドを実行します。")
    return build(api_name, api_version, credentials=creds, static_discovery=False)

def check_youtube_channel(service, target_id):
    """ログイン中のチャンネルがターゲットIDと一致するか確認する"""
    print(f"--- ターゲットチャンネルの確認 (ID: {target_id}) ---")
    
    # 1. まず mine=True で自分が所有するチャンネルを取得
    channels = []
    try:
        results = service.channels().list(mine=True, part='snippet').execute()
        channels = results.get('items', [])
    except Exception as e:
        print(f"[WARN] mine=True での取得に失敗しました: {e}")
    
    # 2. 直接ID指定でも取得を試みる（ブランドアカウント対策）
    try:
        id_results = service.channels().list(id=target_id, part='snippet').execute()
        id_channels = id_results.get('items', [])
        # 重複を避けてマージ
        existing_ids = [c['id'] for c in channels]
        for c in id_channels:
            if c['id'] not in existing_ids:
                channels.append(c)
    except Exception as e:
        print(f"[WARN] ID指定での取得に失敗しました: {e}")
        
    for channel in channels:
        if channel['id'] == target_id:
            print(f"[OK] 成功: '{channel['snippet']['title']}' として認証されています。")
            return True
            
    print("[ERROR] エラー: 指定されたYouTubeチャンネルが認証リストに見つかりません。")
    if channels:
        print("現在アクセス可能なチャンネル:")
        for c in channels:
            print(f" - {c['snippet']['title']} (ID: {c['id']})")
    return False

def get_channel_context(service, target_id):
    """
    重複防止と学習のために、対象チャンネルの直近の動画と人気動画のタイトルを取得する。
    """
    if not target_id:
        return ""
        
    try:
        print(f"--- チャンネルの学習データを取得中 (ID: {target_id}) ---")
        context_str = ""
        
        # 1. 直近の動画（最新15件）
        recent_req = service.search().list(
            part='snippet', channelId=target_id, order='date', type='video', maxResults=15
        )
        recent_res = recent_req.execute()
        recent_titles = [item['snippet']['title'] for item in recent_res.get('items', [])]
        
        if recent_titles:
            context_str += "【Recent Topics (DO NOT REPEAT)】\n"
            for t in recent_titles:
                context_str += f"- {t}\n"
                
        # 2. 人気の動画（再生回数トップ5件）
        top_req = service.search().list(
            part='snippet', channelId=target_id, order='viewCount', type='video', maxResults=5
        )
        top_res = top_req.execute()
        top_titles = [item['snippet']['title'] for item in top_res.get('items', [])]
        
        if top_titles:
            context_str += "\n【Top Performing Topics (USE AS INSPIRATION)】\n"
            for t in top_titles:
                context_str += f"- {t}\n"
                
        return context_str
    except Exception as e:
        print(f"学習データの取得に失敗しました: {e}")
        return ""

def fetch_latest_task(service, list_name="My Tasks"):
    """ToDoリストから最新のタスクを取得する"""
    lists = service.tasklists().list().execute().get('items', [])
    target_list = next((l for l in lists if l['title'] == list_name), lists[0] if lists else None)
    
    if not target_list: return None
    
    tasks = service.tasks().list(tasklist=target_list['id']).execute().get('items', [])
    if not tasks: return None
    
    # 完了していない最新のタスクを返す
    for task in tasks:
        if task.get('status') != 'completed' and task.get('notes'):
            return task
    return None

def upload_to_youtube(service, video_file, title, description, tags):
    """YouTubeにアップロードする"""
    print(f"--- YouTubeアップロード開始: {title} ---")
    
    if not os.path.exists(video_file):
        print(f"[ERROR] 動画ファイルが存在しません: {video_file}")
        raise FileNotFoundError(f"動画ファイルが見つかりません: {video_file}")
        
    print(f"[DEBUG] 対象ファイルパス: {video_file} (実在を確認)")
    
    body = {
        'snippet': {
            'title': title,
            'description': f"{description}\n\n{tags}",
            'tags': ['Shorts'] + tags.replace('#', '').split(),
            'categoryId': '22'
        },
        'status': {
            'privacyStatus': 'public',
            'selfDeclaredMadeForKids': False
        }
    }
    print(f"[INFO] プライバシー設定: {body['status']['privacyStatus']}")
    
    try:
        media = MediaFileUpload(video_file, chunksize=-1, resumable=True, mimetype='video/mp4')
        request = service.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"[INFO] アップロード進行中: {int(status.progress() * 100)}%")
                
        print(f"[OK] アップロード完了！ 動画ID: {response.get('id')}")
        return response.get('id')
    except HttpError as e:
        print(f"[ERROR] API HTTPエラー発生: コード {e.resp.status}")
        print(f"[ERROR] 詳細: {e.content.decode('utf-8')}")
        if e.resp.status in [401, 403]:
            print("[ERROR] 🚨 認証エラーまたはQuota（API利用枠）の超過が発生しています。")
            print("  -> 対策: refresh_tokenが有効か、またはGoogle Cloud Consoleで上限に達していないか確認してください。")
        raise
    except Exception as e:
        print(f"[ERROR] 予期せぬアップロードエラー: {e}")
        raise

async def main():
    # 引数からプロファイルを取得 (デフォルトはmacro)
    profile_key = sys.argv[1] if len(sys.argv) > 1 else 'macro'
    config = load_config()
    
    if profile_key not in config:
        print(f"エラー: プロファイル '{profile_key}' が見つかりません。")
        return

    p = config[profile_key]
    print(f"=== モード開始: {p['profile_name']} ({profile_key}) ===")
    
    try:
        # 1. 認証の取得 (TasksとYouTubeを分離してエラー回避)
        tasks_token = f"tokens/tasks_{profile_key}.pickle"
        youtube_token = f"tokens/youtube_{profile_key}.pickle"
        
        # Tasksの認証（メールアカウント）
        tasks_service = get_authenticated_service('tasks', 'v1', SCOPES_TASKS, tasks_token)
        print("✅ Google Tasks の認証に成功しました。")
        
        # YouTubeの認証（ブランドアカウント）
        youtube_service = get_authenticated_service('youtube', 'v3', SCOPES_YOUTUBE, youtube_token)
        
        # 2. チャンネル確認 (ペットチャンネル等でIDが空の場合は一覧を表示する)
        if not p['channel_id']:
            print("\n--- 現在のYouTubeチャンネルリスト ---")
            results = youtube_service.channels().list(mine=True, part='snippet').execute()
            for c in results.get('items', []):
                print(f"名前: {c['snippet']['title']}, ID: {c['id']}")
            print("\n※正しいIDを config.json の channel_id に記入してください。")
            return

        if not check_youtube_channel(youtube_service, p['channel_id']):
            print("中止します。正しいアカウントでログインし直してください。")
            return

        # 3. タスク取得
        task = fetch_latest_task(tasks_service, p['task_list_name'])
        if not task:
            print(f"エラー: ToDoリスト '{p['task_list_name']}' に未完了のタスクが見つかりませんでした。")
            return
        
        print(f"タスク取得成功: {task['title']}")
        
        # 4. 動画生成
        video_file, _ = await generate_video.make_short_video(
            task['notes'], 
            'bg.jpg', 
            p['bgm'], 
            "youtube_short.mp4",
            voice=p['voice']
        )
        
        # 5. YouTubeアップロード
        upload_to_youtube(youtube_service, video_file, task['title'], task['notes'], p['tags'])
        
        print("\n=== すべての工程が正常に完了しました！ ===")

    except Exception as e:
        print(f"エラーが発生しました: {e}")
        # 詳細なエラー情報を出すために、スタックトレースを表示
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
