#!/usr/bin/env python3
# 测试数据库管理模块功能

import sys
import os

# 添加插件目录到Python路径
sys.path.insert(0, os.path.dirname(__file__))

from databases.db_manager import DatabaseManager

def test_database_init():
    """测试数据库初始化"""
    print("\n=== 测试数据库初始化 ===")
    
    db_manager = DatabaseManager()
    db_manager.initialize()
    print("数据库初始化成功")
    return db_manager

def test_store_data(db_manager):
    """测试存储数据"""
    print("\n=== 测试存储数据 ===")
    
    # 测试1：存储B站视频观后感
    result = db_manager.store_plugin_data(
        plugin_name="bilibili_watcher",
        data_type="video_summary",
        content="这是一个关于AI的视频，主要讲了大语言模型的发展历程和未来趋势。视频内容非常精彩，讲解清晰易懂，推荐大家观看。",
        metadata={"video_url": "https://www.bilibili.com/video/BV1xx411c7mC", "title": "AI大语言模型发展历程"}
    )
    print(f"存储B站视频观后感结果: {result}")
    
    # 测试2：存储小说阅读笔记
    result = db_manager.store_plugin_data(
        plugin_name="novel_reader",
        data_type="chapter_notes",
        content="第一章主要介绍了主角的背景和故事的开端，主角是一个普通的大学生，意外获得了超能力。情节设置合理，人物形象鲜明。",
        metadata={"novel_name": "超能力大学生", "chapter": "第一章"}
    )
    print(f"存储小说阅读笔记结果: {result}")

def test_query_data(db_manager):
    """测试查询数据"""
    print("\n=== 测试查询数据 ===")
    
    # 测试1：查询所有数据
    print("\n1. 查询所有数据:")
    all_data = db_manager.get_all_plugin_data()
    print(f"总共存储了 {len(all_data)} 条数据")
    
    # 测试2：按关键词查询
    print("\n2. 按关键词'AI'查询:")
    ai_data = db_manager.query_plugin_data("AI")
    print(f"找到 {len(ai_data)} 条包含'AI'的数据")
    for item in ai_data:
        print(f"  - {item['plugin_name']}/{item['data_type']}: {item['content'][:50]}...")
    
    # 测试3：按插件名称查询
    print("\n3. 按插件名称'bilibili_watcher'查询:")
    bili_data = db_manager.query_plugin_data("", "bilibili_watcher")
    print(f"找到 {len(bili_data)} 条bilibili_watcher的数据")

def test_backup(db_manager):
    """测试备份功能"""
    print("\n=== 测试备份功能 ===")
    
    # 测试1：手动备份
    print("\n1. 执行手动备份:")
    backup_result = db_manager.backup()
    print(f"备份结果: {backup_result}")
    
    # 测试2：获取备份列表
    print("\n2. 获取备份列表:")
    backup_list = db_manager.get_backup_list()
    print(f"总共有 {len(backup_list)} 个备份")
    for backup in backup_list:
        print(f"  - {backup['filename']} (创建时间: {backup['mtime']}, 大小: {backup['size']}字节)")

def test_relations(db_manager):
    """测试关系管理功能"""
    print("\n=== 测试关系管理功能 ===")
    
    # 测试1：更新关系
    print("\n1. 更新关系:")
    result = db_manager.update_relation(
        user_id="user123",
        group_id="group456",
        nickname="张三",
        favor_change=5,
        impression="这是一个友好的用户，经常分享有用的信息。"
    )
    print(f"更新关系结果: {result}")
    
    # 测试2：查询关系
    print("\n2. 查询关系:")
    relations = db_manager.get_all_relations()
    print(f"总共有 {len(relations)} 条关系记录")
    for relation in relations:
        print(f"  - {relation['nickname']} (好感度: {relation['favor_level']})")

def main():
    """主测试函数"""
    print("开始测试数据库管理模块功能...")
    
    try:
        # 测试数据库初始化
        db_manager = test_database_init()
        
        # 测试存储数据
        test_store_data(db_manager)
        
        # 测试查询数据
        test_query_data(db_manager)
        
        # 测试关系管理
        test_relations(db_manager)
        
        # 测试备份功能
        test_backup(db_manager)
        
        print("\n=== 测试完成 ===")
        print("数据库管理模块功能测试成功！")
        
    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 关闭数据库连接
        if 'db_manager' in locals():
            db_manager.close()

if __name__ == "__main__":
    main()
