package com.campus.studyroom.task;

import com.campus.studyroom.service.VisionService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 视觉状态同步定时任务：轮询视觉服务，更新座位实时状态
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class VisionSyncTask {

    private final VisionService visionService;

    @Scheduled(fixedDelayString = "${studyroom.vision.poll-interval-ms:5000}")
    public void sync() {
        try {
            visionService.syncSeatStatus();
        } catch (Exception e) {
            log.warn("视觉状态同步异常: {}", e.getMessage());
        }
    }
}
