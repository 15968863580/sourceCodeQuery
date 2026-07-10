"""快速测试 LLM API 连通性。"""
import sys
import httpx

url = "https://oneapi.letright.com.cn/v1/chat/completions"
headers = {
    "Authorization": "Bearer sk-1127741X",
    "Content-Type": "application/json",
}
payload = {
    "model": "qwen3.7-plus",
    "messages": [{"role": "user", "content": "你好，回复OK即可"}],
    "max_tokens": 50,
}

print(f"测试模型: qwen3.7-plus")
try:
    r = httpx.post(url, headers=headers, json=payload, timeout=30)
    print(f"状态码: {r.status_code}")
    print(f"响应: {r.text[:500]}")
except httpx.TimeoutException:
    print("超时！API 30秒内无响应")
    # 尝试其他模型名
    for model in ["qwen-plus", "qwen-max", "qwen-turbo", "gpt-4o-mini"]:
        print(f"\n尝试模型: {model}")
        payload["model"] = model
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=15)
            print(f"状态码: {r.status_code}")
            print(f"响应: {r.text[:300]}")
        except Exception as e:
            print(f"失败: {e}")
except Exception as e:
    print(f"错误: {type(e).__name__}: {e}")
