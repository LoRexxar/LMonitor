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
            
            # 根据任务类型选择处理方式
            if simc_task.task_type == 2:  # 属性模拟
                return self.process_attribute_simulation(simc_task, simc_profile)
            else:  # 常规模拟
                return self.process_regular_simulation(simc_task, simc_profile)
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error processing task {simc_task.id}: {str(e)}")
            simc_task.current_status = 3  # 失败
            simc_task.save()
            return False
        
        return True
    
    def process_regular_simulation(self, simc_task, simc_profile):
        """
        处理常规模拟任务
        :param simc_task: SimcTask对象
        :param simc_profile: SimcProfile对象
        :return: 执行是否成功
        """
        try:
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
                logger.info(f"[SimC Monitor] Regular simulation task {simc_task.id} completed successfully")
            else:
                simc_task.current_status = 3  # 失败
                logger.error(f"[SimC Monitor] Regular simulation task {simc_task.id} failed")
            
            simc_task.save()
            return success
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error in regular simulation for task {simc_task.id}: {str(e)}")
            simc_task.current_status = 3  # 失败
            simc_task.save()
            return False
    
    def process_attribute_simulation(self, simc_task, simc_profile):
        """
        处理属性模拟任务
        :param simc_task: SimcTask对象
        :param simc_profile: SimcProfile对象
        :return: 执行是否成功
        """
        try:
            # 解析属性组合
            selected_attributes = self.parse_selected_attributes(simc_task.ext)
            if len(selected_attributes) != 2:
                logger.error(f"[SimC Monitor] Attribute simulation requires exactly 2 attributes, got {len(selected_attributes)} for task {simc_task.id}")
                simc_task.current_status = 3  # 失败
                simc_task.save()
                return False
            
            # 获取基础属性值
            base_attributes = self.get_base_attributes(simc_profile)
            
            # 获取两个属性及其总和
            attr1, attr2 = selected_attributes[0], selected_attributes[1]
            attr1_base = base_attributes[attr1]
            attr2_base = base_attributes[attr2]
            total_value = attr1_base + attr2_base
            
            logger.info(f"[SimC Monitor] Starting attribute simulation for {attr1} and {attr2}, total: {total_value}, task {simc_task.id}")
            
            # 执行分阶段模拟
            result_files = []
            stage = 0
            
            # 以500为步长进行分配模拟，从attr1=0到attr1=total_value
            # 生成所有需要测试的步长点，确保包含0和total_value
            test_points = list(range(0, total_value, 1000))
            if total_value not in test_points:
                test_points.append(total_value)
            
            for attr1_value in test_points:
                attr2_value = total_value - attr1_value
                
                stage_result_file = f"{simc_task.id}_{attr1}_{attr1_value}_{attr2}_{attr2_value}.html"
                
                # 生成当前阶段的SimC代码
                modified_attributes = base_attributes.copy()
                modified_attributes[attr1] = attr1_value
                modified_attributes[attr2] = attr2_value
                
                simc_code = self.generate_attribute_simc_code(simc_profile, modified_attributes, stage_result_file)
                
                # 创建临时SimC文件
                simc_file_path = os.path.join(self.result_path, f"temp_{simc_task.id}_{stage}.simc")
                
                with open(simc_file_path, 'w', encoding='utf-8') as f:
                    f.write(simc_code)
                
                # 执行SimC命令
                success = self.execute_simc_command(simc_file_path, simc_task, stage_result_file)
                
                # 清理临时文件
                if os.path.exists(simc_file_path):
                    os.remove(simc_file_path)
                
                if success:
                    result_files.append(stage_result_file)
                    logger.info(f"[SimC Monitor] Stage {stage} ({attr1}:{attr1_value}, {attr2}:{attr2_value}) completed for task {simc_task.id}")
                else:
                    logger.error(f"[SimC Monitor] Stage {stage} ({attr1}:{attr1_value}, {attr2}:{attr2_value}) failed for task {simc_task.id}")
                
                stage += 1
            
            # 保存所有结果文件名（以逗号分割）
            simc_task.result_file = ','.join(result_files)
            
            # 更新任务状态
            if result_files:
                simc_task.current_status = 2  # 完成
                logger.info(f"[SimC Monitor] Attribute simulation task {simc_task.id} completed with {len(result_files)} result files")
            else:
                simc_task.current_status = 3  # 失败
                logger.error(f"[SimC Monitor] Attribute simulation task {simc_task.id} failed - no results generated")
            
            simc_task.save()
            return len(result_files) > 0
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error in attribute simulation for task {simc_task.id}: {str(e)}")
            simc_task.current_status = 3  # 失败
            simc_task.save()
            return False

    def generate_simc_code(self, profile, result_file):
        """
        生成SimC代码
        :param profile: SimcProfile对象
        :param result_file: 结果文件名
        :return: 生成的SimC代码字符串
        """
        try:
            # 从数据库获取模板
            from botend.models import SimcTemplate
            template_obj = SimcTemplate.objects.filter(is_active=True).first()
            if not template_obj:
                raise Exception("未找到启用的SimC模板")
            template = template_obj.template_content
            
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

    def execute_simc_command(self, simc_file_path, simc_task, result_file_name=None):
        """
        执行SimC命令
        :param simc_file_path: SimC文件路径
        :param simc_task: SimcTask对象
        :param result_file_name: 自定义结果文件名（可选）
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
                # 使用自定义结果文件名或默认的任务结果文件名
                target_result_file = result_file_name if result_file_name else simc_task.result_file
                result_file_path = os.path.join(self.result_path, target_result_file)
                if os.path.exists(result_file_path):
                    from botend.interface.ossupload import ossUpload
                    try:
                        upload_success = ossUpload(result_file_path)
                        if upload_success:
                            logger.info(f"[SimC Monitor] Result file {target_result_file} uploaded to OSS successfully for task {simc_task.id}")
                        else:
                            logger.error(f"[SimC Monitor] Failed to upload result file {target_result_file} to OSS for task {simc_task.id}")
                    except Exception as e:
                        logger.error(f"[SimC Monitor] Error uploading result file to OSS: {str(e)}")
                else:
                    logger.warning(f"[SimC Monitor] Result file not found: {result_file_path}")
                
                return True
            else:
                logger.error(f"[SimC Monitor] SimC execution failed for task {simc_task.id}")
                logger.error(f"[SimC Monitor] Return code: {result.returncode}")
                
                # 构建错误信息并直接存储到result_file字段
                error_info = f"SimC执行失败\n返回码: {result.returncode}\n"
                if result.stderr:
                    logger.error(f"[SimC Monitor] Error output: {result.stderr}")
                    error_info += f"错误输出: {result.stderr}\n"
                if result.stdout:
                    error_info += f"标准输出: {result.stdout}\n"
                
                # 直接将错误信息存储到result_file字段
                simc_task.result_file = error_info
                simc_task.save()
                return False
                
        except subprocess.TimeoutExpired:
            error_info = f"SimC执行超时\n任务ID: {simc_task.id}\n超时时间: 300秒"
            logger.error(f"[SimC Monitor] SimC execution timeout for task {simc_task.id}")
            # 直接将错误信息存储到result_file字段
            simc_task.result_file = error_info
            simc_task.save()
            return False
        except Exception as e:
            error_info = f"SimC执行异常\n任务ID: {simc_task.id}\n异常信息: {str(e)}"
            logger.error(f"[SimC Monitor] Error executing SimC command: {str(e)}")
            # 直接将错误信息存储到result_file字段
            simc_task.result_file = error_info
            simc_task.save()
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
    
    def parse_selected_attributes(self, ext_data):
        """
        解析选中的属性组合
        :param ext_data: 扩展信息字符串，单个属性组合名称（如'crit_versatility'）
        :return: 选中的属性列表
        """
        try:
            if not ext_data:
                return []
            
            # ext_data是单个属性组合字符串，如"crit_versatility"
            combination = ext_data.strip()
            
            # 属性组合映射
            combination_map = {
                'crit_mastery': ['gear_crit', 'gear_mastery'],
                'crit_haste': ['gear_crit', 'gear_haste'],
                'crit_versatility': ['gear_crit', 'gear_versatility'],
                'mastery_haste': ['gear_mastery', 'gear_haste'],
                'mastery_versatility': ['gear_mastery', 'gear_versatility'],
                'haste_versatility': ['gear_haste', 'gear_versatility'],
                'haste_mastery': ['gear_haste', 'gear_mastery']
            }
            
            # 获取选中的属性
            if combination in combination_map:
                return combination_map[combination]
            else:
                logger.warning(f"[SimC Monitor] Unknown attribute combination: {combination}")
                return []
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error parsing selected attributes: {str(e)}")
            return []
    
    def get_base_attributes(self, simc_profile):
        """
        获取基础属性值
        :param simc_profile: SimcProfile对象
        :return: 属性字典
        """
        return {
            'gear_strength': simc_profile.gear_strength or 93330,
            'gear_crit': simc_profile.gear_crit or 10730,
            'gear_haste': simc_profile.gear_haste or 18641,
            'gear_mastery': simc_profile.gear_mastery or 21785,
            'gear_versatility': simc_profile.gear_versatility or 6757
        }
    
    def generate_attribute_simc_code(self, profile, attributes, result_file):
        """
        生成属性模拟的SimC代码
        :param profile: SimcProfile对象
        :param attributes: 修改后的属性字典
        :param result_file: 结果文件名
        :return: 生成的SimC代码字符串
        """
        try:
            # 从数据库获取模板
            from botend.models import SimcTemplate
            template_obj = SimcTemplate.objects.filter(is_active=True).first()
            if not template_obj:
                raise Exception("未找到启用的SimC模板")
            template = template_obj.template_content
            
            # 替换模板中的占位符
            simc_code = template.replace('{fight_style}', profile.fight_style or 'Patchwerk')
            simc_code = simc_code.replace('{time}', str(profile.time) or '300')
            simc_code = simc_code.replace('{target_count}', str(profile.target_count) or '1')
            simc_code = simc_code.replace('{talent}', profile.talent or '')
            simc_code = simc_code.replace('{action_list}', profile.action_list or '')
            
            # 使用修改后的属性值
            simc_code = simc_code.replace('{gear_strength}', str(attributes['gear_strength']))
            simc_code = simc_code.replace('{gear_crit}', str(attributes['gear_crit']))
            simc_code = simc_code.replace('{gear_haste}', str(attributes['gear_haste']))
            simc_code = simc_code.replace('{gear_mastery}', str(attributes['gear_mastery']))
            simc_code = simc_code.replace('{gear_versatility}', str(attributes['gear_versatility']))
            
            simc_code = simc_code.replace('{result_file}', self.result_path + result_file)
            
            return simc_code
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error generating attribute SimC code: {str(e)}")
            raise e