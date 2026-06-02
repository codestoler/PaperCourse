Title: 创建嵌入请求 - SiliconFlow

URL Source: https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings

Markdown Content:
# 创建嵌入请求 - SiliconFlow

[Skip to main content](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#content-area)

[SiliconFlow home page![Image 1: dark logo](https://mintcdn.com/siliconflow-37161621/A6gMn2s9V_nC5YXV/logo/image.png?fit=max&auto=format&n=A6gMn2s9V_nC5YXV&q=85&s=2dc78b1a5bd0384428a3b28664829b29)![Image 2: dark logo](https://mintcdn.com/siliconflow-37161621/A6gMn2s9V_nC5YXV/logo/image.png?fit=max&auto=format&n=A6gMn2s9V_nC5YXV&q=85&s=2dc78b1a5bd0384428a3b28664829b29)](https://www.siliconflow.cn/)

简体中文

Search...

Ctrl K

Search...

Navigation

文本系列

创建嵌入请求

[用户指南](https://docs.siliconflow.cn/cn/userguide/introduction)[场景示例](https://docs.siliconflow.cn/cn/usercases/use-siliconcloud-in-ClaudeCode)[API手册](https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions)[更新公告](https://docs.siliconflow.cn/cn/release-notes/overview)[条款与协议](https://docs.siliconflow.cn/cn/legals/terms-of-service)

##### 文本系列

*   [POST 创建对话请求（OpenAI）](https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions)
*   [POST 创建对话请求（Anthropic）](https://docs.siliconflow.cn/cn/api-reference/chat-completions/messages)
*   [POST 创建嵌入请求](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings)
*   [POST 创建重排序请求](https://docs.siliconflow.cn/cn/api-reference/rerank/create-rerank)

##### 图像系列

*   [POST 创建图片生成请求](https://docs.siliconflow.cn/cn/api-reference/images/images-generations)

##### 语音系列

*   [POST 上传参考音频](https://docs.siliconflow.cn/cn/api-reference/audio/upload-voice)
*   [POST 创建文本转语音请求](https://docs.siliconflow.cn/cn/api-reference/audio/create-speech)
*   [GET 参考音频列表获取](https://docs.siliconflow.cn/cn/api-reference/audio/voice-list)
*   [POST 删除参考音频](https://docs.siliconflow.cn/cn/api-reference/audio/delete-voice)
*   [POST 创建语音转文本请求](https://docs.siliconflow.cn/cn/api-reference/audio/create-audio-transcriptions)

##### 视频系列

*   [POST 创建视频生成请求](https://docs.siliconflow.cn/cn/api-reference/videos/videos_submit)
*   [POST 获取视频生成链接请求](https://docs.siliconflow.cn/cn/api-reference/videos/get_videos_status)

##### 批量处理

*   [POST 上传文件](https://docs.siliconflow.cn/cn/api-reference/batch/upload-file)
*   [GET 获取文件列表](https://docs.siliconflow.cn/cn/api-reference/batch/get-file-list)
*   [POST 创建batch任务](https://docs.siliconflow.cn/cn/api-reference/batch/create-batch)
*   [GET 获取batch任务详情](https://docs.siliconflow.cn/cn/api-reference/batch/get-batch)
*   [GET 获取batch任务列表](https://docs.siliconflow.cn/cn/api-reference/batch/get-batch-list)
*   [POST 取消batch任务](https://docs.siliconflow.cn/cn/api-reference/batch/cancel-batch)

##### 平台系列

*   [GET 获取用户模型列表](https://docs.siliconflow.cn/cn/api-reference/models/get-model-list)

Create Embeddings

cURL

```
curl --request POST \
  --url https://api.siliconflow.cn/v1/embeddings \
  --header 'Authorization: Bearer <token>' \
  --header 'Content-Type: application/json' \
  --data '
{
  "model": "BAAI/bge-large-zh-v1.5",
  "input": "Silicon flow embedding online: fast, affordable, and high-quality embedding services. come try it out!"
}
'
```

200

400

401

403

404

429

503

504

```
{
  "object": [
    "list"
  ],
  "model": "<string>",
  "data": [
    {
      "object": "embedding",
      "embedding": [
        123
      ],
      "index": 123
    }
  ],
  "usage": {
    "prompt_tokens": 123,
    "completion_tokens": 123,
    "total_tokens": 123
  }
}
```

文本系列

# 创建嵌入请求

Creates an embedding vector representing the input text.

POST

/

embeddings

Create Embeddings

cURL

```
curl --request POST \
  --url https://api.siliconflow.cn/v1/embeddings \
  --header 'Authorization: Bearer <token>' \
  --header 'Content-Type: application/json' \
  --data '
{
  "model": "BAAI/bge-large-zh-v1.5",
  "input": "Silicon flow embedding online: fast, affordable, and high-quality embedding services. come try it out!"
}
'
```

200

400

401

403

404

429

503

504

```
{
  "object": [
    "list"
  ],
  "model": "<string>",
  "data": [
    {
      "object": "embedding",
      "embedding": [
        123
      ],
      "index": 123
    }
  ],
  "usage": {
    "prompt_tokens": 123,
    "completion_tokens": 123,
    "total_tokens": 123
  }
}
```

#### Authorizations

[​](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#authorization-authorization)

Authorization

string

header

required

Use the following format for authentication: Bearer [](https://cloud.siliconflow.cn/account/ak)

#### Body

application/json

[​](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#body-model)

model

string

required

Corresponding Model Name. To better enhance service quality, we will make periodic changes to the models provided by this service, including but not limited to model on/offlining and adjustments to model service capabilities. We will notify you of such changes through appropriate means such as announcements or message pushes where feasible. For a complete list of available models, please check the [Models](https://cloud.siliconflow.cn/sft-d29cs9gh3vvc73c59kb0/models?types=embedding).

Example:

`"BAAI/bge-large-zh-v1.5"`

[​](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#body-input-one-of-0)

input

string string[]

default:Silicon flow embedding online: fast, affordable, and high-quality embedding services. come try it out!

required

Input text to embed must be provided as a string or an array of tokens. To process multiple inputs in a single request, pass an array of strings or an array of token arrays. The input length must not exceed the model's maximum token limit and should not be an empty string. The maximum input tokens for each model are as follows:

BAAI/bge-large-zh-v1.5, BAAI/bge-large-en-v1.5, netease-youdao/bce-embedding-base_v1: 512 BAAI/bge-m3, Pro/BAAI/bge-m3: 8192 Qwen/Qwen3-Embedding-8B, Qwen/Qwen3-Embedding-4B, Qwen/Qwen3-Embedding-0.6B: 32768

Example:

`"Silicon flow embedding online: fast, affordable, and high-quality embedding services. come try it out!"`

[​](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#body-encoding-format)

encoding_format

enum<string>

default:float

"The format to return the embeddings in. Can be either `float` or [`base64`](https://pypi.org/project/pybase64/). "

Available options:

`float`,

`base64`

Example:

`"float"`

[​](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#body-dimensions)

dimensions

integer

The number of dimensions the resulting output embeddings should have. Only supported in `Qwen/Qwen3` series. - Qwen/Qwen3-Embedding-8B: [64,128,256,512,768,1024,1536,2048,2560,4096] - Qwen/Qwen3-Embedding-4B:[64,128,256,512,768,1024,1536,2048,2560] - Qwen/Qwen3-Embedding-0.6B: [64,128,256,512,768,1024]

Example:

`1024`

#### Response

200

application/json

The response from the model. The response header contains the x-siliconcloud-trace-id field, which serves as a unique identifier for tracing requests, facilitating log queries and issue troubleshooting.

[​](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#response-object)

object

enum<string>

required

The object type, which is always "list".

Available options:

`list`

[​](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#response-model)

model

string

required

The name of the model used to generate the embedding.

[​](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#response-data)

data

object[]

required

The list of embeddings generated by the model.

Show child attributes

[​](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings#response-usage)

usage

object

required

The usage information for the request.

Show child attributes

[创建对话请求（Anthropic）](https://docs.siliconflow.cn/cn/api-reference/chat-completions/messages)[创建重排序请求](https://docs.siliconflow.cn/cn/api-reference/rerank/create-rerank)

Ctrl+I

[Powered by This documentation is built and hosted on Mintlify, a developer documentation platform](https://www.mintlify.com/?utm_campaign=poweredBy&utm_medium=referral&utm_source=siliconflow-37161621)


文本系列
创建嵌入请求
Creates an embedding vector representing the input text.

POST
/
embeddings
Authorizations
​
Authorization
stringheaderrequired
Use the following format for authentication: Bearer

Body
application/json
​
model
stringrequired
Corresponding Model Name. To better enhance service quality, we will make periodic changes to the models provided by this service, including but not limited to model on/offlining and adjustments to model service capabilities. We will notify you of such changes through appropriate means such as announcements or message pushes where feasible. For a complete list of available models, please check the Models.

Example:
"BAAI/bge-large-zh-v1.5"

​
input

string
string
default:Silicon flow embedding online: fast, affordable, and high-quality embedding services. come try it out!required
Input text to embed must be provided as a string or an array of tokens. To process multiple inputs in a single request, pass an array of strings or an array of token arrays. The input length must not exceed the model's maximum token limit and should not be an empty string.
The maximum input tokens for each model are as follows:

BAAI/bge-large-zh-v1.5, BAAI/bge-large-en-v1.5, netease-youdao/bce-embedding-base_v1: 512
BAAI/bge-m3, Pro/BAAI/bge-m3: 8192
Qwen/Qwen3-Embedding-8B, Qwen/Qwen3-Embedding-4B, Qwen/Qwen3-Embedding-0.6B: 32768

Example:
"Silicon flow embedding online: fast, affordable, and high-quality embedding services. come try it out!"

​
encoding_format
enum<string>default:float
"The format to return the embeddings in. Can be either float or base64. "

Available options: float, base64 
Example:
"float"

​
dimensions
integer
The number of dimensions the resulting output embeddings should have. Only supported in Qwen/Qwen3 series. - Qwen/Qwen3-Embedding-8B: [64,128,256,512,768,1024,1536,2048,2560,4096] - Qwen/Qwen3-Embedding-4B:[64,128,256,512,768,1024,1536,2048,2560] - Qwen/Qwen3-Embedding-0.6B: [64,128,256,512,768,1024]

Example:
1024

Response

200

application/json
The response from the model. The response header contains the x-siliconcloud-trace-id field, which serves as a unique identifier for tracing requests, facilitating log queries and issue troubleshooting.

​
object
enum<string>required
The object type, which is always "list".

Available options: list 
​
model
stringrequired
The name of the model used to generate the embedding.

​
data
object[]required
The list of embeddings generated by the model.

Show child attributes

​
usage
objectrequired
The usage information for the request.

Show child attributes