import os
from urllib.parse import quote
from django.conf import settings as django_settings
from utils.log import logger

OSS_CONFIG = getattr(django_settings, 'OSS_CONFIG', {}) or {}

def _build_public_url(object_key: str) -> str:
    base_url = (OSS_CONFIG.get("base_url") or "").strip()
    if not base_url:
        return ""
    encoded_key = "/".join(quote(part) for part in str(object_key or "").split("/"))
    return "{}{}".format(base_url.rstrip("/") + "/", encoded_key)


def ossUploadObject(file_path: str, object_key: str = ""):

    """
    Python SDK V2 客户端初始化配置说明：

    1. 签名版本：Python SDK V2 默认使用 V4 签名，提供更高的安全性
    2. Region配置：初始化 Client 时，必须指定阿里云 Region ID 作为请求地域标识，例如华东1（杭州）Region ID：cn-hangzhou
    3. Endpoint配置：
       - 可通过Endpoint参数自定义服务请求的访问域名
       - 当不指定 Endpoint 时，将根据 Region 自动构造公网访问域名，例如Region为cn-hangzhou时，构造访问域名为：https://oss-cn-hangzhou.aliyuncs.com
    4. 协议配置：
       - SDK 默认使用 HTTPS 协议构造访问域名
       - 如需使用 HTTP 协议，在指定域名时明确指定：http://oss-cn-hangzhou.aliyuncs.com
    """

    object_key = (object_key or os.path.basename(file_path)).lstrip("/")
    if not object_key:
        logger.error("文件上传失败: object_key 为空")
        return ""
    required_keys = ["access_key_id", "access_key_secret", "region", "bucket_name", "base_url"]
    missing_keys = [key for key in required_keys if not OSS_CONFIG.get(key)]
    if missing_keys:
        logger.warning("文件上传跳过: OSS_CONFIG 缺少 {}".format(",".join(missing_keys)))
        return ""
    try:
        import alibabacloud_oss_v2 as oss
    except Exception as e:
        logger.error("文件上传失败: OSS SDK 未安装 {}".format(str(e)))
        return ""

    # 从环境变量中加载凭证信息，用于身份验证
    credentials_provider = oss.credentials.StaticCredentialsProvider(
        access_key_id=OSS_CONFIG["access_key_id"],
        access_key_secret=OSS_CONFIG["access_key_secret"]
    )


    # 加载SDK的默认配置，并设置凭证提供者
    cfg = oss.config.load_default()
    cfg.credentials_provider = credentials_provider

    # 方式一：只填写Region（推荐）
    # 必须指定Region ID，以华东1（杭州）为例，Region填写为cn-hangzhou，SDK会根据Region自动构造HTTPS访问域名
    cfg.region = OSS_CONFIG["region"] 

    

    # # 方式二：同时填写Region和Endpoint
    # # 必须指定Region ID，以华东1（杭州）为例，Region填写为cn-hangzhou
    # cfg.region = 'cn-hangzhou'
    # # 填写Bucket所在地域对应的公网Endpoint。以华东1（杭州）为例，Endpoint填写为'https://oss-cn-hangzhou.aliyuncs.com'
    # cfg.endpoint = 'https://oss-cn-hangzhou.aliyuncs.com'

    # 设置不使用https请求
    cfg.disable_ssl = True

    # 使用配置好的信息创建OSS客户端
    client = oss.Client(cfg)

    # 定义要上传的字符串内容
    text_string = "Hello, OSS!"
    data = text_string.encode('utf-8')  # 将字符串编码为UTF-8字节串

    # 执行上传对象的请求，直接从本地文件上传
    # 指定存储空间名称、对象名称和本地文件路径
    with open(file_path, 'rb') as f:
        result = client.put_object(
            oss.PutObjectRequest(
                bucket=OSS_CONFIG["bucket_name"],  # 存储空间名称
                key=object_key,        # 对象名称
                body=f.read()        # 读取文件内容
            )
        )

    # 输出请求的结果信息，包括状态码、请求ID、内容MD5、ETag、CRC64校验码、版本ID和服务器响应时间
    if result.status_code == 200:
        logger.info("文件上传成功")
        return _build_public_url(object_key)
    else:
        logger.error("文件上传失败")
        return ""


def ossUpload(file_path: str):
    return bool(ossUploadObject(file_path))

# 当此脚本被直接运行时，调用main函数
if __name__ == "__main__":
    main()  # 脚本入口，当文件被直接运行时调用main函数
