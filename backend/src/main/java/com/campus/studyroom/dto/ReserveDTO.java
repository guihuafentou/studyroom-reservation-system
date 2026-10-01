package com.campus.studyroom.dto;

import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;
import lombok.Data;

import java.time.LocalDate;
import java.util.List;

/**
 * 创建预约请求：一个座位 + 日期 + 1~N 个连续时段
 */
@Data
public class ReserveDTO {

    @NotNull(message = "座位不能为空")
    private Long seatId;

    @NotNull(message = "日期不能为空")
    private LocalDate date;

    @NotEmpty(message = "请选择至少一个时段")
    private List<Long> slotIds;
}
