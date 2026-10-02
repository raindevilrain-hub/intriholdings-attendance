# 인트리홀딩스 출근 자동 체크

하이웍스(Hiworks) 출퇴근을 자동화하는 Windows 트레이 앱. 사내망에서만 동작하며, Windows 로그인 시 자동 출근 체크, 컴퓨터 종료 시 퇴근 체크 확인을 처리한다.

사용 안내는 [안내.txt](안내.txt), 관리자 안내는 [관리자용_안내.txt](관리자용_안내.txt) 참고.

## 빌드

```
py -m PyInstaller --noconfirm --clean IntriHoldingsAttendance.spec
```

## 배포

`인트리홀딩스_출근자동체크_설치.exe`를 더블클릭하면 설치/업데이트된다. 설치된 앱은 [Releases](../../releases)의 최신 버전을 하루 한 번 자동으로 확인해 스스로 업데이트한다.
