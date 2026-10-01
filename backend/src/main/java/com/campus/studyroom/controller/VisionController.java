package com.campus.studyroom.controller;

import com.campus.studyroom.common.Result;
import com.campus.studyroom.security.RequireRole;
import com.campus.studyroom.service.VisionService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

/**
 * 视觉识别接口
 */
@RestController
@RequestMapping("/api/vision")
@RequiredArgsConstructor
public class VisionController {

    private final VisionService visionService;

    /** 某自习室座位实时状态（前端轮询） */
    @GetMapping("/status")
    public Result<List<Map<String, Object>>> status(@RequestParam Long roomId) {
        return Result.ok(visionService.visionStatus(roomId));
    }

    /** 视觉服务健康状态（管理员） */
    @GetMapping("/service/status")
    @RequireRole(1)
    public Result<Map<String, Object>> serviceStatus() {
        return Result.ok(visionService.serviceStatus());
    }

    /** 触发背景重置（管理员） */
    @PostMapping("/reset")
    @RequireRole(1)
    public Result<Void> reset() {
        visionService.resetBackground();
        return Result.ok();
    }
}
