#!/usr/bin/env python3
"""
Test vLLM OpenAI-compatible API
"""

import os
import sys
import json
import argparse
from typing import Optional

try:
    import requests
except ImportError:
    print("Error: requests library not found. Install with: pip install requests")
    sys.exit(1)


class VLLMTester:
    def __init__(self, base_url: str, api_key: Optional[str] = None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key or "dummy-key"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def test_health(self):
        """Test health endpoint."""
        print("Testing /health endpoint...")
        try:
            response = requests.get(f"{self.base_url}/health", timeout=5)
            if response.status_code == 200:
                print("✓ Health check passed")
                return True
            else:
                print(f"✗ Health check failed: {response.status_code}")
                return False
        except Exception as e:
            print(f"✗ Health check failed: {e}")
            return False

    def test_models(self):
        """Test /v1/models endpoint."""
        print("\nTesting /v1/models endpoint...")
        try:
            response = requests.get(
                f"{self.base_url}/v1/models",
                headers=self.headers,
                timeout=10
            )
            if response.status_code == 200:
                models = response.json()
                print(f"✓ Models endpoint working")
                print(f"  Available models: {len(models.get('data', []))}")
                for model in models.get('data', []):
                    print(f"    - {model.get('id')}")
                return True
            else:
                print(f"✗ Models endpoint failed: {response.status_code}")
                print(f"  Response: {response.text}")
                return False
        except Exception as e:
            print(f"✗ Models endpoint failed: {e}")
            return False

    def test_completion(self):
        """Test /v1/completions endpoint."""
        print("\nTesting /v1/completions endpoint...")
        payload = {
            "model": "deepseek-coder-6.7b-instruct",
            "prompt": "def fibonacci(n):",
            "max_tokens": 100,
            "temperature": 0.2,
            "stop": ["\n\n"]
        }

        try:
            response = requests.post(
                f"{self.base_url}/v1/completions",
                headers=self.headers,
                json=payload,
                timeout=30
            )
            if response.status_code == 200:
                result = response.json()
                completion = result['choices'][0]['text']
                print("✓ Completion endpoint working")
                print(f"  Prompt: {payload['prompt']}")
                print(f"  Completion: {completion[:100]}...")
                return True
            else:
                print(f"✗ Completion endpoint failed: {response.status_code}")
                print(f"  Response: {response.text}")
                return False
        except Exception as e:
            print(f"✗ Completion endpoint failed: {e}")
            return False

    def test_chat_completion(self):
        """Test /v1/chat/completions endpoint."""
        print("\nTesting /v1/chat/completions endpoint...")
        payload = {
            "model": "deepseek-coder-6.7b-instruct",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a helpful coding assistant."
                },
                {
                    "role": "user",
                    "content": "Write a Python function to check if a number is prime."
                }
            ],
            "max_tokens": 200,
            "temperature": 0.2
        }

        try:
            response = requests.post(
                f"{self.base_url}/v1/chat/completions",
                headers=self.headers,
                json=payload,
                timeout=30
            )
            if response.status_code == 200:
                result = response.json()
                message = result['choices'][0]['message']['content']
                print("✓ Chat completion endpoint working")
                print(f"  Question: {payload['messages'][1]['content']}")
                print(f"  Answer: {message[:150]}...")
                return True
            else:
                print(f"✗ Chat completion endpoint failed: {response.status_code}")
                print(f"  Response: {response.text}")
                return False
        except Exception as e:
            print(f"✗ Chat completion endpoint failed: {e}")
            return False

    def run_all_tests(self):
        """Run all tests."""
        print("=" * 60)
        print("vLLM API Test Suite")
        print("=" * 60)
        print(f"Base URL: {self.base_url}")
        print()

        results = []
        results.append(("Health Check", self.test_health()))
        results.append(("Models Endpoint", self.test_models()))
        results.append(("Completion Endpoint", self.test_completion()))
        results.append(("Chat Completion Endpoint", self.test_chat_completion()))

        print("\n" + "=" * 60)
        print("Test Results Summary")
        print("=" * 60)

        passed = sum(1 for _, result in results if result)
        total = len(results)

        for name, result in results:
            status = "✓ PASS" if result else "✗ FAIL"
            print(f"{status} - {name}")

        print()
        print(f"Total: {passed}/{total} tests passed")

        return passed == total


def main():
    parser = argparse.ArgumentParser(description="Test vLLM OpenAI-compatible API")
    parser.add_argument(
        "--url",
        default=os.getenv("VLLM_URL", "http://localhost:8000"),
        help="vLLM base URL (default: http://localhost:8000)"
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("OPENAI_API_KEY"),
        help="API key (optional)"
    )

    args = parser.parse_args()

    tester = VLLMTester(args.url, args.api_key)
    success = tester.run_all_tests()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
