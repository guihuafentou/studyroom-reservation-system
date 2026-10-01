package com.campus.studyroom.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.studyroom.common.BusinessException;
import com.campus.studyroom.dto.LoginDTO;
import com.campus.studyroom.dto.RegisterDTO;
import com.campus.studyroom.entity.User;
import com.campus.studyroom.mapper.UserMapper;
import com.campus.studyroom.security.JwtUtil;
import com.campus.studyroom.security.UserContext;
import lombok.RequiredArgsConstructor;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.HashMap;
import java.util.Map;

/**
 * 认证服务：注册 / 登录 / 当前用户
 */
@Service
@RequiredArgsConstructor
public class AuthService {

    private final UserMapper userMapper;
    private final JwtUtil jwtUtil;
    private final BCryptPasswordEncoder encoder = new BCryptPasswordEncoder();

    public User register(RegisterDTO dto) {
        Long count = userMapper.selectCount(new LambdaQueryWrapper<User>()
                .eq(User::getStudentNo, dto.getStudentNo()));
        if (count != null && count > 0) {
            throw BusinessException.conflict("该学号已注册");
        }
        count = userMapper.selectCount(new LambdaQueryWrapper<User>()
                .eq(User::getUsername, dto.getUsername()));
        if (count != null && count > 0) {
            throw BusinessException.conflict("用户名已存在");
        }

        User user = new User();
        user.setStudentNo(dto.getStudentNo());
        user.setUsername(dto.getUsername());
        user.setPassword(encoder.encode(dto.getPassword()));
        user.setRealName(dto.getRealName());
        user.setRole(User.ROLE_STUDENT);
        user.setStatus(1);
        user.setViolationCount(0);
        user.setCreateTime(LocalDateTime.now());
        userMapper.insert(user);
        return user;
    }

    public Map<String, Object> login(LoginDTO dto) {
        User user = userMapper.selectOne(new LambdaQueryWrapper<User>()
                .eq(User::getUsername, dto.getUsername()));
        if (user == null || !encoder.matches(dto.getPassword(), user.getPassword())) {
            throw BusinessException.badRequest("用户名或密码错误");
        }
        if (user.getStatus() == null || user.getStatus() == 0) {
            throw BusinessException.forbidden("账号已被禁用，请联系管理员");
        }
        String token = jwtUtil.createToken(user.getId(), user.getUsername(), user.getRole());
        Map<String, Object> data = new HashMap<>();
        data.put("token", token);
        data.put("user", user);
        return data;
    }

    public User me() {
        User user = userMapper.selectById(UserContext.getUserId());
        if (user == null) {
            throw BusinessException.unauthorized("用户不存在");
        }
        return user;
    }
}
