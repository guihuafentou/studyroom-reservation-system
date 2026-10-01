package com.campus.studyroom;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 校园自习室预约管理系统 - 启动类
 */
@SpringBootApplication
@EnableScheduling
@MapperScan("com.campus.studyroom.mapper")
public class StudyRoomApplication {

    public static void main(String[] args) {
        SpringApplication.run(StudyRoomApplication.class, args);
        System.out.println("===== 自习室预约管理系统后端启动成功: http://localhost:8080 =====");
    }
}
