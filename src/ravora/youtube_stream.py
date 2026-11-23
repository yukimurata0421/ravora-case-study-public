"""YouTube's TLS server-streaming RPC. Only normalized public event fields leave here."""
from contextlib import contextmanager
import re
import threading

from ravora.providers import ProviderError

ENDPOINT = 'dns:///youtube.googleapis.com:443'
METHOD = '/youtube.api.v3.V3DataLiveChatMessageService/StreamList'
EVENT_NAMES = {'TEXT_MESSAGE_EVENT': 'textMessageEvent', 'TOMBSTONE': 'tombstone',
               'CHAT_ENDED_EVENT': 'chatEndedEvent', 'USER_BANNED_EVENT': 'userBannedEvent'}

def _rpc_error(error):
    code = 'YOUTUBE_GRPC_' + error.code().name
    failure = ProviderError(code)
    if code != 'YOUTUBE_GRPC_RESOURCE_EXHAUSTED':
        return failure
    # Providers may attach structured retry/quota instructions. Never expose their raw metadata.
    try:
        from google.rpc import error_details_pb2, status_pb2
        from google.protobuf.message import DecodeError
    except ImportError:
        return failure
    for key, value in error.trailing_metadata() or ():
        if key != 'grpc-status-details-bin' or not isinstance(value, bytes) or len(value) > 65536:
            continue
        try:
            status = status_pb2.Status.FromString(value)
            if status.code != 8:
                continue
            for detail in status.details:
                info = error_details_pb2.ErrorInfo()
                retry = error_details_pb2.RetryInfo()
                if detail.Unpack(info) and info.reason.replace('_','').lower() == 'dailylimitexceeded':
                    return ProviderError('YOUTUBE_DAILY_LIMIT_EXCEEDED')
                if detail.Unpack(retry):
                    delay = retry.retry_delay.seconds + retry.retry_delay.nanos / 1e9
                    failure.retry_after = max(delay, getattr(failure,'retry_after',0))
        except DecodeError:
            continue
    return failure


@contextmanager
def stream_list(settings, chat, cursor, timeout, stop, *, access_token=None):
    if not settings.youtube_read_enabled or not (access_token or settings.youtube_key):
        raise ProviderError('YOUTUBE_STREAM_CREDENTIALS_REQUIRED')
    if not access_token and not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', settings.youtube_key):
        raise ProviderError('INVALID_YOUTUBE_API_KEY')
    try:
        import grpc
        from google.protobuf.json_format import MessageToDict
        from ravora.youtube_proto.stream_list_pb2 import LiveChatMessageListRequest, LiveChatMessageListResponse
    except ImportError:
        raise ProviderError('YOUTUBE_GRPC_DEPENDENCY_MISSING') from None

    request = LiveChatMessageListRequest(live_chat_id=chat, part=['id', 'snippet', 'authorDetails'])
    if cursor:
        request.page_token = cursor
    options = [('grpc.enable_retries', 0), ('grpc.max_receive_message_length', 2 * 1024 * 1024)]
    with grpc.secure_channel(ENDPOINT, grpc.ssl_channel_credentials(), options=options) as channel:
        rpc = channel.unary_stream(METHOD, request_serializer=LiveChatMessageListRequest.SerializeToString,
                                   response_deserializer=LiveChatMessageListResponse.FromString)
        metadata = (('authorization', 'Bearer ' + access_token()),) if access_token else (('x-goog-api-key', settings.youtube_key),)
        call = rpc(request, metadata=metadata, timeout=timeout)
        done = threading.Event()

        def cancel_on_stop():
            while not done.wait(.08):
                if stop.is_set():
                    call.cancel()
                    return

        def pages():
            try:
                for message in call:
                    page = MessageToDict(message)
                    for item in page.get('items', []):
                        snippet = item.get('snippet', {})
                        snippet['type'] = EVENT_NAMES.get(snippet.get('type'), 'unsupported')
                    yield page
            except grpc.RpcError as error:
                # The remote detail string and debug metadata are never public diagnostics.
                raise _rpc_error(error) from None

        watcher = threading.Thread(target=cancel_on_stop, daemon=True)
        watcher.start()
        try:
            yield pages()
        finally:
            done.set()
            call.cancel()
            watcher.join()
