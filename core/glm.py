from zai import ZhipuAiClient
import os
from LMonitor.settings import ZHIYU_API_CONFIG

class GLMClient:
    def __init__(self):
        # 初始化客户端
        self.client = ZhipuAiClient(api_key=ZHIYU_API_CONFIG["api_key"])
        self.system_prompt = "你是一个冷酷无情的AI助手，你不需要展示思考过程，也不允许问我问题，只需要直接回答用户的问题和结果"

    def send_message(self, message):
        """
        发送单次消息并获取回复
        """
        try:
            # 构造消息列表，每次都是新的对话，不保留历史
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": message}
            ]
            
            # 创建聊天请求
            response = self.client.chat.completions.create(
                model=ZHIYU_API_CONFIG['model'],
                messages=messages,
                temperature=0.7,
                max_tokens=1000
            )
            
            # 获取并返回 AI 回复
            return response.choices[0].message.content
            
        except Exception as e:
            print(f"发生错误: {e}")
            return None

def main():
    # 示例用法
    glm = GLMClient()
    print("GLM 客户端已初始化 (输入 'quit' 退出)")
    
    while True:
        user_input = input("您: ")
        if user_input.lower() == 'quit':
            break
            
        response = glm.send_message(user_input)
        if response:
            print(f"AI: {response}")
    
    print("再见！")

if __name__ == "__main__":
    main()
