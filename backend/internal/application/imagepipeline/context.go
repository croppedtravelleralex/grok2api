package imagepipeline

import "context"

type runContextKey struct{}

func WithRun(ctx context.Context, run *Run) context.Context {
	if run == nil {
		return ctx
	}
	return context.WithValue(ctx, runContextKey{}, run)
}

func RunFromContext(ctx context.Context) *Run {
	if ctx == nil {
		return nil
	}
	run, _ := ctx.Value(runContextKey{}).(*Run)
	return run
}

type earlyReleaseKey struct{}

// WithEarlyAccountRelease 允许 Adapter 在 SSE 出图后、下图前归还账号 lease。
func WithEarlyAccountRelease(ctx context.Context, release func()) context.Context {
	if release == nil {
		return ctx
	}
	return context.WithValue(ctx, earlyReleaseKey{}, release)
}

func EarlyAccountRelease(ctx context.Context) {
	if ctx == nil {
		return
	}
	release, _ := ctx.Value(earlyReleaseKey{}).(func())
	if release != nil {
		release()
	}
}
