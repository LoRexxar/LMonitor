#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: SimcMonitor.py
@time: 2025/1/15 10:00
@desc: SimC模拟监控

'''

import os
import subprocess
import hashlib
import time
from django.conf import settings
from utils.log import logger
from botend.models import SimcTask, SimcProfile
from botend.controller.BaseScan import BaseScan


class SimcMonitor(BaseScan):
    """
    SimC模拟监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)
        
        self.task = task
        self.hint = ""
        
        # 从settings获取SimC配置
        self.simc_config = getattr(settings, 'SIMC_CONFIG', {})
        self.simc_path = self.simc_config.get('simc_path', '')
        self.result_path = os.path.join(os.getcwd(), self.simc_config.get('result_path', 'static/simc_results/'))
        self.simc_template_path = self.simc_config.get('simc_template', 'LMonitor/simc_template.txt')
        
        # 确保结果目录存在
        if not os.path.exists(self.result_path):
            os.makedirs(self.result_path, exist_ok=True)

    def scan(self, url=None):
        """
        执行SimC模拟扫描
        :param url: 可选参数，这里不使用
        :return:
        """
        logger.info("[SimC Monitor] Start SimC simulation check.")
        
        try:
            # 检查SimC路径是否正确
            if not self.simc_path:
                logger.error(f"[SimC Monitor] SimC path not configured")
                return False
            if not os.path.exists(self.simc_path):
                logger.error(f"[SimC Monitor] SimC executable not found at path: {self.simc_path}")
                return False
            if not os.path.isfile(self.simc_path):
                logger.error(f"[SimC Monitor] SimC path is not a file: {self.simc_path}")
                return False

            # 获取所有活跃的SimC任务
            simc_tasks = SimcTask.objects.filter(is_active=True, current_status=0)
            
            for task in simc_tasks:
                logger.info(f"[SimC Monitor] Processing task: {task.name} (ID: {task.id})")
                self.process_simc_task(task)
                
        except Exception as e:
            logger.error(f"[SimC Monitor] Error during SimC simulation: {str(e)}")
            return False
            
        return True

    def process_simc_task(self, simc_task):
        """
        处理单个SimC任务
        :param simc_task: SimcTask对象
        :return:
        """
        try:
            # 更新任务状态为进行中
            simc_task.current_status = 1
            simc_task.save()
            
            # 获取SimC配置
            simc_profile = SimcProfile.objects.filter(
                id=simc_task.simc_profile_id,
                user_id=simc_task.user_id,
                is_active=True
            ).first()
            
            if not simc_profile:
                logger.error(f"[SimC Monitor] SimC profile not found for task {simc_task.id}")
                simc_task.current_status = 3  # 失败
                simc_task.save()
                return False
            
            # 生成SimC代码
            simc_code = self.generate_simc_code(simc_profile, simc_task.result_file)
            
            # 创建临时SimC文件
            simc_file_path = os.path.join(self.result_path, f"temp_{simc_task.id}.simc")
            
            with open(simc_file_path, 'w', encoding='utf-8') as f:
                f.write(simc_code)
            
            # 执行SimC命令
            success = self.execute_simc_command(simc_file_path, simc_task)
            
            # 清理临时文件
            if os.path.exists(simc_file_path):
                os.remove(simc_file_path)
            
            # 更新任务状态
            if success:
                simc_task.current_status = 2  # 完成
                logger.info(f"[SimC Monitor] Task {simc_task.id} completed successfully")
            else:
                simc_task.current_status = 3  # 失败
                logger.error(f"[SimC Monitor] Task {simc_task.id} failed")
            
            simc_task.save()
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error processing task {simc_task.id}: {str(e)}")
            simc_task.current_status = 3  # 失败
            simc_task.save()
            return False
        
        return True

    def generate_simc_code(self, profile, result_file):
        """
        生成SimC代码
        :param profile: SimcProfile对象
        :param result_file: 结果文件名
        :return: 生成的SimC代码字符串
        """
        try:
            # 读取模板文件
            with open(self.simc_template_path, 'r', encoding='utf-8') as f:
                template = f.read()
            
            # 替换模板中的占位符
            simc_code = template.replace('{fight_style}', profile.fight_style or 'Patchwerk')
            simc_code = simc_code.replace('{time}', str(profile.time) or '300')
            simc_code = simc_code.replace('{target_count}', str(profile.target_count) or '1')
            simc_code = simc_code.replace('{talent}', profile.talent or '')
            simc_code = simc_code.replace('{action_list}', profile.action_list or '')
            simc_code = simc_code.replace('{gear_strength}', str(profile.gear_strength) or '93330')
            simc_code = simc_code.replace('{gear_crit}', str(profile.gear_crit) or '10730')
            simc_code = simc_code.replace('{gear_haste}', str(profile.gear_haste) or '18641')
            simc_code = simc_code.replace('{gear_mastery}', str(profile.gear_mastery) or '21785')
            simc_code = simc_code.replace('{gear_versatility}', str(profile.gear_versatility) or '6757')
            simc_code = simc_code.replace('{result_file}', self.result_path + result_file)
            
            return simc_code
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error generating SimC code: {str(e)}")
            raise e

    def execute_simc_command(self, simc_file_path, simc_task):
        """
        执行SimC命令
        :param simc_file_path: SimC文件路径
        :param simc_task: SimcTask对象
        :return: 执行是否成功
        """
        try:
            
            # 构建命令
            cmd = [self.simc_path, simc_file_path]
            
            logger.info(f"[SimC Monitor] Executing command: {' '.join(cmd)}")
            
            # 执行命令
            result = subprocess.run(
                cmd,
                cwd=self.result_path,
                capture_output=True,
                text=True,
                timeout=300  # 5分钟超时
            )
            
            if result.returncode == 0:
                logger.info(f"[SimC Monitor] SimC execution successful for task {simc_task.id}")
                if result.stdout:
                    logger.debug(f"[SimC Monitor] SimC output: {result.stdout[:500]}...")  # 只记录前500字符
                
                # 上传结果文件到OSS
                result_file_path = os.path.join(self.result_path, simc_task.result_file)
                if os.path.exists(result_file_path):
                    from botend.interface.ossupload import ossUpload
                    try:
                        upload_success = ossUpload(result_file_path)
                        if upload_success:
                            logger.info(f"[SimC Monitor] Result file uploaded to OSS successfully for task {simc_task.id}")
                        else:
                            logger.error(f"[SimC Monitor] Failed to upload result file to OSS for task {simc_task.id}")
                    except Exception as e:
                        logger.error(f"[SimC Monitor] Error uploading result file to OSS: {str(e)}")
                else:
                    logger.warning(f"[SimC Monitor] Result file not found: {result_file_path}")
                
                return True
            else:
                logger.error(f"[SimC Monitor] SimC execution failed for task {simc_task.id}")
                logger.error(f"[SimC Monitor] Return code: {result.returncode}")
                if result.stderr:
                    logger.error(f"[SimC Monitor] Error output: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            logger.error(f"[SimC Monitor] SimC execution timeout for task {simc_task.id}")
            return False
        except Exception as e:
            logger.error(f"[SimC Monitor] Error executing SimC command: {str(e)}")
            return False

    def check_status(self, result):
        """
        检查请求状态
        :param result: 请求结果
        :return: 状态检查结果
        """
        return True

    def resolve_data(self, result):
        """
        处理返回的内容
        :param result: 返回内容
        :return: 处理结果
        """
        return True

    def trigger_webhook(self):
        """
        触发webhook
        :return: 触发结果
        """
        return True